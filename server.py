from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen
import argparse
import cgi
import csv
import gzip
import json
import os
import re
import uuid


ROOT = Path(__file__).resolve().parent
FR24_API_URL = "https://fr24api.flightradar24.com"
FR24_TOKEN_ENV = "FR24_API_TOKEN"
FR24API_FEED_URL = "https://data-cloud.flightradar24.com/zones/fcgi/feed.js"
FR24API_CLICKHANDLER_URL = "https://data-live.flightradar24.com/clickhandler/"
DATA_DIR = ROOT / "data"
UPLOAD_DIR = ROOT / "uploads"
PROOF_DIR = ROOT / "proofs"
SHIPMENTS_FILE = DATA_DIR / "shipments.json"
LOGS_FILE = DATA_DIR / "arrival_logs.csv"

AIRPORTS = {
    "GRU": {"name": "Guarulhos", "city": "Sao Paulo", "lat": -23.4356, "lon": -46.4731},
    "VCP": {"name": "Viracopos", "city": "Campinas", "lat": -23.0074, "lon": -47.1345},
    "BSB": {"name": "Brasilia", "city": "Brasilia", "lat": -15.8697, "lon": -47.9208},
    "GIG": {"name": "Galeao", "city": "Rio de Janeiro", "lat": -22.8099, "lon": -43.2505},
    "CNF": {"name": "Confins", "city": "Belo Horizonte", "lat": -19.6244, "lon": -43.9719},
    "JPA": {"name": "Castro Pinto", "city": "Joao Pessoa", "lat": -7.1458, "lon": -34.9486},
}


def send_json(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def load_shipments():
    if not SHIPMENTS_FILE.exists():
        return []
    try:
        return json.loads(SHIPMENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def visible_shipments():
    archived = {"documented", "archived"}
    return [
        shipment for shipment in load_shipments()
        if shipment.get("active", True) and shipment.get("status") not in archived
    ]


def save_shipments(shipments):
    DATA_DIR.mkdir(exist_ok=True)
    SHIPMENTS_FILE.write_text(json.dumps(shipments, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_filename(filename):
    name = Path(filename or "minuta.pdf").name
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "minuta.pdf"


def csv_value(value):
    return "" if value is None else str(value)


def append_log(event, shipment, proof_url=""):
    DATA_DIR.mkdir(exist_ok=True)
    columns = [
        "timestamp",
        "event",
        "tracking",
        "origin",
        "destination",
        "carrier",
        "recipient",
        "cargo_type",
        "previous_status",
        "new_status",
        "proof_url",
        "source_file",
    ]
    exists = LOGS_FILE.exists()
    with LOGS_FILE.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if not exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "tracking": csv_value(shipment.get("tracking")),
            "origin": csv_value(shipment.get("origin_code")),
            "destination": csv_value(shipment.get("destination_code")),
            "carrier": csv_value(shipment.get("carrier")),
            "recipient": csv_value(shipment.get("recipient")),
            "cargo_type": csv_value(shipment.get("cargo_type")),
            "previous_status": csv_value(shipment.get("previous_status")),
            "new_status": csv_value(shipment.get("status")),
            "proof_url": proof_url,
            "source_file": csv_value(shipment.get("source_file")),
        })


class MissingFlightRadarToken(RuntimeError):
    pass


def parse_fr24_reference(value):
    text = unquote(str(value or "").strip())
    clean = text.split("?", 1)[0].split("#", 1)[0].strip()
    parsed = urlparse(clean if "://" in clean else f"https://local/{clean.lstrip('/')}")
    path = parsed.path.strip("/")
    parts = [part for part in path.split("/") if part]

    if parsed.netloc and "flightradar24.com" in parsed.netloc.lower():
        candidates = parts
    elif "/" in clean:
        candidates = [part for part in clean.strip("/").split("/") if part]
    else:
        candidates = re.split(r"[-\s]+", clean)

    flight = ""
    flight_id = ""
    for part in candidates:
        token = re.sub(r"[^A-Za-z0-9]", "", part)
        if not flight and re.fullmatch(r"[A-Za-z]{2,4}\d{1,5}[A-Za-z]?", token):
            flight = token.upper()
        if not flight_id and re.fullmatch(r"[0-9A-Fa-f]{6,10}", token):
            flight_id = token.lower()

    if not flight:
        match = re.search(r"\b([A-Za-z]{2,4}\d{1,5}[A-Za-z]?)\b", clean)
        flight = match.group(1).upper() if match else ""
    if not flight_id:
        match = re.search(r"\b([0-9A-Fa-f]{6,10})\b", clean)
        flight_id = match.group(1).lower() if match else ""

    return {
        "raw": text,
        "flight": flight,
        "flight_id": flight_id,
        "display": "/".join(part for part in [flight, flight_id] if part) or text,
    }


def fr24_request(path, params):
    token = os.environ.get(FR24_TOKEN_ENV, "").strip()
    if not token:
        raise MissingFlightRadarToken(f"Configure {FR24_TOKEN_ENV} no servidor local.")

    query = urlencode({key: value for key, value in params.items() if value not in ("", None)})
    url = f"{FR24_API_URL}{path}"
    if query:
        url = f"{url}?{query}"

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Version": "v1",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Rastreio-Franquia-Aerea-Local/0.2",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"FlightRadar respondeu HTTP {exc.code}: {detail[:240]}") from exc


def fr24api_request(url, params):
    query = urlencode({key: value for key, value in params.items() if value not in ("", None)})
    full_url = f"{url}?{query}" if query else url
    request = Request(
        full_url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Origin": "https://www.flightradar24.com",
            "Referer": "https://www.flightradar24.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        },
    )
    try:
        with urlopen(request, timeout=16) as response:
            body = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return json.loads(body.decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"FlightRadarAPI respondeu HTTP {exc.code}: {detail[:240]}") from exc


def safe_list_get(items, index, default=None):
    try:
        value = items[index]
    except (IndexError, TypeError):
        return default
    return default if value in ("", None, "N/A") else value


def fr24api_record_from_feed(flight_id, info, reference):
    lat = safe_list_get(info, 1)
    lon = safe_list_get(info, 2)
    if lat is None or lon is None:
        return None
    flight = safe_list_get(info, 13) or safe_list_get(info, 16) or reference.get("flight") or flight_id
    return {
        "source": "FlightRadarAPI",
        "fr24_id": flight_id,
        "hex": safe_list_get(info, 0) or flight_id,
        "flight": flight,
        "callsign": safe_list_get(info, 16),
        "registration": safe_list_get(info, 9),
        "type": safe_list_get(info, 8) or "Aeronave",
        "origin": safe_list_get(info, 11),
        "destination": safe_list_get(info, 12),
        "lat": lat,
        "lon": lon,
        "alt": safe_list_get(info, 4, 0),
        "speed": safe_list_get(info, 5, 0),
        "track": safe_list_get(info, 3, 0),
        "timestamp": safe_list_get(info, 10),
        "on_ground": safe_list_get(info, 14),
        "vertical_speed": safe_list_get(info, 15, 0),
    }


def fr24api_record_from_details(payload, reference):
    if not isinstance(payload, dict):
        return None

    trail = payload.get("trail") or []
    points = [
        point for point in trail
        if point.get("lat") is not None and (point.get("lng") is not None or point.get("lon") is not None)
    ]
    latest = max(points, key=lambda item: item.get("ts") or item.get("timestamp") or 0) if points else {}
    if not latest:
        return None

    identification = payload.get("identification") or {}
    number = identification.get("number") or {}
    aircraft = payload.get("aircraft") or {}
    model = aircraft.get("model") or {}
    airport = payload.get("airport") or {}
    origin = (airport.get("origin") or {}).get("code") or {}
    destination = (airport.get("destination") or {}).get("code") or {}
    status = payload.get("status") or {}
    flight = (
        number.get("default")
        or number.get("alternative")
        or identification.get("callsign")
        or reference.get("flight")
        or reference.get("flight_id")
    )
    return {
        "source": "FlightRadarAPI",
        "fr24_id": reference.get("flight_id") or identification.get("id"),
        "hex": aircraft.get("hex") or reference.get("flight_id") or flight,
        "flight": flight,
        "callsign": identification.get("callsign"),
        "registration": aircraft.get("registration"),
        "type": model.get("code") or model.get("text") or "Aeronave",
        "origin": origin.get("iata") or origin.get("icao"),
        "destination": destination.get("iata") or destination.get("icao"),
        "lat": latest.get("lat"),
        "lon": latest.get("lng") if latest.get("lng") is not None else latest.get("lon"),
        "alt": latest.get("alt"),
        "speed": latest.get("spd"),
        "track": latest.get("hd"),
        "timestamp": latest.get("ts") or latest.get("timestamp"),
        "status": status.get("text"),
        "eta": (payload.get("time") or {}).get("estimated", {}).get("arrival"),
        "track_count": len(points),
    }


def get_fr24api_live(reference):
    records = []
    raw = {}
    feed_error = ""

    if reference.get("flight_id"):
        try:
            details = fr24api_request(FR24API_CLICKHANDLER_URL, {"flight": reference["flight_id"]})
            raw["clickhandler"] = details
            details_record = fr24api_record_from_details(details, reference)
            if details_record:
                records.append(details_record)
        except Exception as exc:
            raw["clickhandler_error"] = str(exc)

    feed_params = {
        "faa": "1",
        "satellite": "1",
        "mlat": "1",
        "flarm": "1",
        "adsb": "1",
        "gnd": "1",
        "air": "1",
        "vehicles": "0",
        "estimated": "1",
        "maxage": "14400",
        "gliders": "1",
        "stats": "1",
        "limit": "5000",
    }
    try:
        feed = fr24api_request(FR24API_FEED_URL, feed_params)
    except Exception as exc:
        feed = {}
        feed_error = str(exc)
    raw["feed"] = {"full_count": len(feed) if isinstance(feed, dict) else 0, "error": feed_error}

    candidates = []
    if isinstance(feed, dict):
        target_id = (reference.get("flight_id") or "").lower()
        target_flight = (reference.get("flight") or "").upper()
        for flight_id, info in feed.items():
            if not isinstance(info, list):
                continue
            flight_number = str(safe_list_get(info, 13, "") or "").upper()
            callsign = str(safe_list_get(info, 16, "") or "").upper()
            if target_id and str(flight_id).lower() == target_id:
                candidates.insert(0, (flight_id, info))
            elif target_flight and target_flight in {flight_number, callsign}:
                candidates.append((flight_id, info))

    for flight_id, info in candidates[:3]:
        record = fr24api_record_from_feed(flight_id, info, reference)
        if record:
            records.insert(0, record)

    seen = set()
    unique = []
    for record in records:
        key = record.get("fr24_id") or record.get("hex") or record.get("flight")
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    if not unique and (raw.get("clickhandler_error") or feed_error):
        raise RuntimeError(raw.get("clickhandler_error") or feed_error)
    return unique, raw


def fr24_record_from_live(record, reference):
    if not record or record.get("lat") is None or record.get("lon") is None:
        return None
    flight = record.get("flight") or record.get("callsign") or reference.get("flight") or record.get("fr24_id")
    return {
        "source": "Flightradar24",
        "fr24_id": record.get("fr24_id") or reference.get("flight_id"),
        "hex": record.get("hex") or record.get("fr24_id") or reference.get("flight_id") or flight,
        "flight": flight or "FR24",
        "callsign": record.get("callsign"),
        "registration": record.get("reg"),
        "type": record.get("type") or "Aeronave",
        "origin": record.get("orig_iata") or record.get("orig_icao"),
        "destination": record.get("dest_iata") or record.get("dest_icao"),
        "lat": record.get("lat"),
        "lon": record.get("lon"),
        "alt": record.get("alt"),
        "speed": record.get("gspeed"),
        "track": record.get("track"),
        "timestamp": record.get("timestamp"),
    }


def fr24_record_from_tracks(payload, reference):
    tracks = payload.get("tracks") if isinstance(payload, dict) else []
    if not tracks:
        return None

    latest = next(
        (track for track in reversed(tracks) if track.get("lat") is not None and track.get("lon") is not None),
        None,
    )
    if not latest:
        return None

    flight = payload.get("flight") or payload.get("callsign") or reference.get("flight") or payload.get("fr24_id")
    return {
        "source": "Flightradar24",
        "fr24_id": payload.get("fr24_id") or reference.get("flight_id"),
        "hex": payload.get("hex") or payload.get("fr24_id") or reference.get("flight_id") or flight,
        "flight": flight or "FR24",
        "callsign": payload.get("callsign"),
        "registration": payload.get("reg"),
        "type": payload.get("type") or "Aeronave",
        "origin": payload.get("orig_iata") or payload.get("orig_icao"),
        "destination": payload.get("dest_iata") or payload.get("dest_icao"),
        "lat": latest.get("lat"),
        "lon": latest.get("lon"),
        "alt": latest.get("alt"),
        "speed": latest.get("gspeed"),
        "track": latest.get("track"),
        "timestamp": latest.get("timestamp"),
        "track_count": len(tracks),
    }


def read_logs():
    if not LOGS_FILE.exists():
        return []
    with LOGS_FILE.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_multipart_file(handler, field_name):
    content_type = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        raise ValueError("Envie o arquivo como multipart/form-data.")

    form = cgi.FieldStorage(
        fp=handler.rfile,
        headers=handler.headers,
        environ={
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": handler.headers.get("Content-Length", "0"),
        },
    )
    field = form[field_name] if field_name in form else None
    if field is None or not getattr(field, "filename", ""):
        raise ValueError("Nenhum arquivo recebido.")
    data = field.file.read()
    if not data:
        raise ValueError("Arquivo vazio.")
    return clean_filename(field.filename), data


def extract_pdf_text(data):
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("Leitor de PDF indisponivel neste ambiente.") from exc

    with fitz.open(stream=data, filetype="pdf") as document:
        return "\n".join(page.get_text("text") for page in document)


def first_after(lines, start_text):
    try:
        start = next(index for index, line in enumerate(lines) if line == start_text)
    except StopIteration:
        return ""
    for line in lines[start + 1:]:
        if line:
            return line
    return ""


def section_field(lines, upper_lines, section, field):
    try:
        section_index = next(index for index, line in enumerate(upper_lines) if line == section)
    except StopIteration:
        return ""

    try:
        field_index = next(
            index for index in range(section_index + 1, len(upper_lines))
            if upper_lines[index] == field
        )
    except StopIteration:
        return ""

    labels = {
        "NOME/RAZAO SOCIAL",
        "CNPJ/CPF",
        "IE",
        "E-MAIL",
        "TELEFONE",
        "ENDERECO",
        "BAIARO",
        "CIDADE - UF",
        "COMPLEMENTO",
        "CEP",
    }
    for candidate, candidate_upper in zip(lines[field_index + 1:], upper_lines[field_index + 1:]):
        if candidate_upper not in labels:
            return candidate
    return ""


def extract_declared_value(text):
    match = re.search(r"R\$\s*([0-9][0-9.,]*)", text)
    return match.group(1) if match else ""


def parse_minuta_text(text, filename):
    raw_lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in raw_lines if line]
    upper_lines = [line.upper() for line in lines]
    full_upper = "\n".join(upper_lines)

    airport_codes = []
    for line in upper_lines:
        match = re.fullmatch(r"([A-Z]{3})\s*-", line)
        if match and match.group(1) in AIRPORTS:
            airport_codes.append(match.group(1))

    origin_code = airport_codes[0] if airport_codes else "GRU"
    destination_code = airport_codes[1] if len(airport_codes) > 1 else origin_code

    tracking = ""
    for index, line in enumerate(upper_lines):
        if re.fullmatch(r"\d{7,12}", line):
            nearby = "\n".join(upper_lines[max(0, index - 4):index + 5])
            if origin_code in nearby or destination_code in nearby:
                tracking = line
                break

    if not tracking:
        ignored_lengths = {8, 10, 11, 13, 14}
        candidates = re.findall(r"\b\d{7,12}\b", full_upper)
        for candidate in candidates:
            if len(candidate) not in ignored_lengths:
                tracking = candidate
                break
        tracking = tracking or (candidates[0] if candidates else f"MINUTA-{uuid.uuid4().hex[:6].upper()}")

    carrier = "Latam Cargos" if "LATAM" in full_upper else "Franquia aerea"
    cargo_type = "Cartoes" if "CARTOES" in full_upper else first_after(upper_lines, "TIPO DE EMBALAGEM").title()
    service_type = first_after(upper_lines, "TIPO DE SERVICO")
    sender = section_field(lines, upper_lines, "REMETENTE", "NOME/RAZAO SOCIAL")
    sender_document = section_field(lines, upper_lines, "REMETENTE", "CNPJ/CPF")
    recipient = section_field(lines, upper_lines, "DESTINATARIO", "NOME/RAZAO SOCIAL")
    recipient_document = section_field(lines, upper_lines, "DESTINATARIO", "CNPJ/CPF")
    recipient_city = section_field(lines, upper_lines, "DESTINATARIO", "CIDADE - UF")
    declared_value = extract_declared_value(text)
    emitted_at = ""
    timestamp = re.search(r"\b\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}\b", text)
    if timestamp:
        emitted_at = timestamp.group(0)

    volumes = ""
    for index, line in enumerate(upper_lines):
        if line == "SACA":
            for candidate in reversed(upper_lines[max(0, index - 4):index]):
                if re.fullmatch(r"\d+", candidate):
                    volumes = candidate
                    break
            break

    origin_name = AIRPORTS.get(origin_code, {}).get("name", origin_code)
    destination_name = AIRPORTS.get(destination_code, {}).get("name", destination_code)
    if tracking in upper_lines:
        track_index = upper_lines.index(tracking)
        if track_index + 1 < len(lines):
            origin_name = lines[track_index + 1].title()
        if track_index + 2 < len(lines):
            destination_name = lines[track_index + 2].title()

    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": f"ship-{tracking}",
        "tracking": tracking,
        "manifest_code": "",
        "fr24_id": "",
        "fr24_url": "",
        "carrier": carrier,
        "origin_code": origin_code,
        "origin_name": origin_name,
        "destination_code": destination_code,
        "destination_name": destination_name,
        "recipient": recipient,
        "recipient_document": recipient_document,
        "recipient_city": recipient_city,
        "sender": sender,
        "sender_document": sender_document,
        "cargo_type": cargo_type,
        "service_type": service_type,
        "volumes": volumes,
        "declared_value": declared_value,
        "value_source": "minuta" if declared_value else "pendente",
        "status": "sent",
        "active": True,
        "emitted_at": emitted_at,
        "source_file": filename,
        "created_at": now,
        "updated_at": now,
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/aircraft":
            send_json(self, 200, {"ac": [], "source": "compat"})
            return
        if parsed.path == "/api/fr24/resolve":
            self.resolve_fr24(parsed.query)
            return
        if parsed.path == "/api/shipments":
            self.list_shipments()
            return
        if parsed.path == "/api/logs":
            self.list_logs()
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/minutas":
            self.create_minuta()
            return
        match = re.fullmatch(r"/api/shipments/([^/]+)/document-arrival", parsed.path)
        if match:
            self.document_arrival(match.group(1))
            return
        send_json(self, 404, {"error": "Endpoint nao encontrado."})

    def list_shipments(self):
        params = parse_qs(urlparse(self.path).query)
        shipments = load_shipments() if params.get("all", ["0"])[0] == "1" else visible_shipments()
        send_json(self, 200, {"shipments": shipments})

    def list_logs(self):
        send_json(self, 200, {"logs": read_logs(), "csv": "/data/arrival_logs.csv"})

    def resolve_fr24(self, query):
        params = parse_qs(query)
        reference = parse_fr24_reference(params.get("ref", [""])[0])
        if not reference["flight"] and not reference["flight_id"]:
            send_json(self, 400, {"error": "Informe voo, link ou id do FlightRadar.", "reference": reference})
            return

        aircraft = []
        raw_source = {}
        errors = []
        try:
            aircraft, raw_source["flightradarapi"] = get_fr24api_live(reference)
        except Exception as exc:
            errors.append(str(exc))

        try:
            if not aircraft and reference["flight"]:
                live = fr24_request("/api/live/flight-positions/full", {"flights": reference["flight"]})
                raw_source["live"] = live
                records = live.get("data", []) if isinstance(live, dict) else []
                if isinstance(records, dict):
                    records = [records]
                if reference["flight_id"]:
                    records = [
                        record for record in records
                        if str(record.get("fr24_id", "")).lower() == reference["flight_id"]
                    ] or records
                aircraft = [fr24_record_from_live(record, reference) for record in records]
                aircraft = [record for record in aircraft if record]

            if not aircraft and reference["flight_id"]:
                tracks = fr24_request("/api/flight-tracks", {"flight_id": reference["flight_id"]})
                raw_source["tracks"] = tracks
                tracked_aircraft = fr24_record_from_tracks(tracks, reference)
                aircraft = [tracked_aircraft] if tracked_aircraft else []
        except MissingFlightRadarToken as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(str(exc))

        if not aircraft and errors:
            send_json(self, 502, {
                "error": errors[0],
                "errors": errors,
                "reference": reference,
                "aircraft": [],
                "source": "FlightRadarAPI",
            })
            return

        send_json(self, 200, {
            "source": "FlightRadarAPI",
            "reference": reference,
            "aircraft": aircraft,
            "record_count": len(aircraft),
            "raw": raw_source,
        })

    def create_minuta(self):
        try:
            filename, data = read_multipart_file(self, "minuta")
        except ValueError as exc:
            send_json(self, 400, {"error": str(exc)})
            return

        UPLOAD_DIR.mkdir(exist_ok=True)
        saved_name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{filename}"
        saved_path = UPLOAD_DIR / saved_name
        saved_path.write_bytes(data)

        try:
            if data[:4] != b"%PDF":
                raise ValueError("Nesta etapa a leitura automatica aceita PDF. OCR de imagem fica para a proxima camada.")
            text = extract_pdf_text(data)
            shipment = parse_minuta_text(text, filename)
        except Exception as exc:
            send_json(self, 422, {"error": str(exc), "source_file": filename})
            return

        shipment["stored_file"] = saved_name
        shipments = load_shipments()
        shipments = [item for item in shipments if item.get("tracking") != shipment["tracking"]]
        shipments.insert(0, shipment)
        save_shipments(shipments)
        append_log("Minuta cadastrada", shipment)

        send_json(self, 201, {"shipment": shipment, "shipments": shipments})

    def document_arrival(self, shipment_id):
        try:
            filename, data = read_multipart_file(self, "proof")
        except ValueError as exc:
            send_json(self, 400, {"error": str(exc)})
            return

        if data[:4] != b"%PDF":
            send_json(self, 422, {"error": "Comprovante precisa ser PDF."})
            return

        shipments = load_shipments()
        shipment_index = next(
            (index for index, item in enumerate(shipments) if item.get("id") == shipment_id),
            None,
        )
        if shipment_index is None:
            send_json(self, 404, {"error": "Remessa nao encontrada."})
            return

        PROOF_DIR.mkdir(exist_ok=True)
        saved_name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{shipment_id}-{filename}"
        saved_path = PROOF_DIR / clean_filename(saved_name)
        saved_path.write_bytes(data)
        proof_url = f"/proofs/{saved_path.name}"

        previous = shipments[shipment_index].get("status", "")
        shipments[shipment_index]["previous_status"] = previous
        shipments[shipment_index]["status"] = "documented"
        shipments[shipment_index]["active"] = False
        shipments[shipment_index]["arrival_documented_at"] = datetime.now(timezone.utc).isoformat()
        shipments[shipment_index]["arrival_proof_file"] = saved_path.name
        shipments[shipment_index]["arrival_proof_url"] = proof_url
        shipments[shipment_index]["updated_at"] = shipments[shipment_index]["arrival_documented_at"]
        save_shipments(shipments)
        append_log("Chegada documentada", shipments[shipment_index], proof_url)

        send_json(self, 200, {
            "shipment": shipments[shipment_index],
            "proof_url": proof_url,
            "logs": read_logs(),
        })

def main():
    parser = argparse.ArgumentParser(description="Rastreio Franquia Aerea local server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Rastreio Franquia Aerea: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
