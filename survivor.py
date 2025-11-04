# survivor.py
from __future__ import annotations

import os
import json
import sqlite3
import hashlib
from typing import Dict, Tuple, List, Optional
from datetime import datetime, date, time, timezone

import requests
import pytz
import typer
from dotenv import load_dotenv

app = typer.Typer(add_completion=False)

# ===================== ENV & CONFIG =====================

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
LEAGUE_ID = os.getenv("LEAGUE_ID", "")
USERNAME = os.getenv("USERNAME", "")
PASSWORD = os.getenv("PASSWORD", "")
API_TOKEN = os.getenv("API_TOKEN", "")
DEFAULT_TZ = os.getenv("TZ", "America/Lima")

SQLITE_PATH = os.getenv("SQLITE_PATH", "survivor.db")
RESULTS_PATH = os.getenv("MATCHES_RESULTS_PATH", "/matches/results/").strip()

NFL_LOGO_URL = "https://static.www.nfl.com/t_headshot_desktop/f_auto/league/api/clubs/logos/NFL"


def require_env() -> None:
    missing = []
    if not BASE_URL:
        missing.append("BASE_URL")
    if not LEAGUE_ID:
        missing.append("LEAGUE_ID")
    if not (API_TOKEN or (USERNAME and PASSWORD)):
        missing.append("API_TOKEN o USERNAME/PASSWORD")
    if missing:
        raise RuntimeError(f"Falta {' ,'.join(missing)} en .env")


def login_and_token() -> str:
    """
    Devuelve un Bearer token. Si API_TOKEN existe en .env, lo usa directo.
    Caso contrario, hace login (FORM DATA) en /auth/login/.
    """
    if API_TOKEN:
        return API_TOKEN

    url = f"{BASE_URL}/auth/login/"
    r = requests.post(
        url,
        data={
            "username": USERNAME,
            "password": PASSWORD,
            # Si tu backend requiere más campos de OAuth2, agrégalos aquí.
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    try:
        r.raise_for_status()
    except requests.HTTPError:
        raise RuntimeError(f"Login failed {r.status_code}: {r.text}")

    data = r.json()
    token = (
        data.get("access_token")
        or data.get("access")
        or data.get("token")
        or ""
    )
    if not token:
        raise RuntimeError(f"Login OK pero no encontré token en respuesta: {data}")
    return token


def auth_headers() -> dict:
    token = login_and_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ===================== DB HELPERS =====================


def ensure_db() -> None:
    con = sqlite3.connect(SQLITE_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS matches (
            api_id TEXT PRIMARY KEY,
            external_id TEXT,
            home TEXT,
            away TEXT,
            start_time_utc TEXT,
            week INTEGER
        )
        """
    )
    con.commit()
    con.close()


def save_match(api_id: str, external_id: str, home: str, away: str, start_utc: str, week: int) -> None:
    ensure_db()
    con = sqlite3.connect(SQLITE_PATH)
    cur = con.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO matches (api_id, external_id, home, away, start_time_utc, week)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (api_id, external_id, home, away, start_utc, week),
    )
    con.commit()
    con.close()


def load_matches_by_week(week: int) -> List[Dict]:
    ensure_db()
    con = sqlite3.connect(SQLITE_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT api_id, home, away, start_time_utc FROM matches WHERE week=? ORDER BY start_time_utc",
        (week,),
    )
    rows = cur.fetchall()
    con.close()
    return [{"api_id": r[0], "home": r[1], "away": r[2], "start_time_utc": r[3]} for r in rows]

# ===================== TEAMS / TIME HELPERS =====================


def load_teams_index(teams_json_path: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Devuelve:
      - by_short: dict short_name -> team_uuid
      - by_name: dict full name -> team_uuid
    Compatible con el fixture estilo Django que me pasaste.
    """
    with open(teams_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    by_short: Dict[str, str] = {}
    by_name: Dict[str, str] = {}
    for row in data:
        fields = row.get("fields", {})
        short = (fields.get("short_name") or "").strip().upper()
        name = (fields.get("name") or "").strip()
        pk = row.get("pk")
        if short and pk:
            by_short[short] = pk
        if name and pk:
            by_name[name] = pk
    return by_short, by_name


def parse_time_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(hour=int(hh), minute=int(mm))


def local_to_utc(dt_local: datetime, tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    localized = tz.localize(dt_local)
    return localized.astimezone(timezone.utc)


def iso_utc(dt_utc: datetime) -> str:
    # Ejemplo del Swagger incluía microsegundos + 'Z'
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

# ===================== CREATE MATCHES =====================


@app.command("create-matches")
def create_matches(
    season: str = typer.Option(..., help="Temporada, ej. 2025"),
    week: int = typer.Option(..., help="Semana (int)"),
    date_str: str = typer.Option(..., "--date-str", help="Fecha local YYYY-MM-DD"),
    times: str = typer.Option(..., help='Horas locales "HH:MM,HH:MM,...", p.ej. "17:55,18:00,18:05"'),
    pairs: str = typer.Option(..., help='Pares "HOME@AWAY,HOME@AWAY,...", p.ej. "BUF@MIA,NE@NYJ,KC@LAC"'),
    teams_json_path: str = typer.Option(..., "--teams-json-path", help="Ruta a teams.json"),
    tz_name: str = typer.Option(DEFAULT_TZ, "--tz-name", help="Zona horaria local, default: America/Lima"),
    dry_run: bool = typer.Option(False, help="No enviar a la API, solo mostrar payloads"),
):
    """
    Crea partidos (POST /matches/) y guarda el id del backend en SQLite.
    """
    require_env()
    headers = auth_headers()
    by_short, _ = load_teams_index(teams_json_path)

    pair_list = [p.strip() for p in pairs.split(",") if p.strip()]
    time_list = [t.strip() for t in times.split(",") if t.strip()]

    if len(pair_list) != len(time_list):
        raise typer.BadParameter(
            f"pairs ({len(pair_list)}) y times ({len(time_list)}) deben tener el mismo número de items."
        )

    created = 0
    errors = 0
    for i, (pair, hhmm) in enumerate(zip(pair_list, time_list), start=1):
        try:
            home_short, away_short = [x.strip().upper() for x in pair.split("@")]
            if home_short not in by_short or away_short not in by_short:
                raise ValueError(f"Equipo no encontrado en teams.json: {pair}")

            home_id = by_short[home_short]
            away_id = by_short[away_short]

            d = date.fromisoformat(date_str)
            t = parse_time_hhmm(hhmm)
            start_local = datetime.combine(d, t)
            start_utc = local_to_utc(start_local, tz_name)

            # -------- external_id de 32 chars (MD5) --------
            eid_src = f"{home_short}-{away_short}-{start_utc.strftime('%Y%m%d%H%M')}"
            external_id = hashlib.md5(eid_src.encode()).hexdigest()  # 32 chars exactos

            payload = {
                "external_id": external_id,
                "league_id": LEAGUE_ID,
                "home_id": home_id,
                "away_id": away_id,
                "season": str(season),
                "week": int(week),
                "start_time": iso_utc(start_utc),
            }

            typer.echo(f"[{i}] POST /matches -> {payload}")

            if dry_run:
                created += 1
                continue

            url = f"{BASE_URL}/matches/"
            r = requests.post(url, headers=headers, json=payload, timeout=30)
            if r.status_code not in (200, 201):
                typer.echo(f"[ERR {r.status_code}] {r.text}", err=True)
                errors += 1
                continue

            data = r.json()
            api_id = data.get("id") or data.get("pk") or ""
            if not api_id:
                typer.echo(f"[WARN] Respuesta sin id: {data}", err=True)
            else:
                save_match(
                    api_id=api_id,
                    external_id=external_id,
                    home=home_short,
                    away=away_short,
                    start_utc=payload["start_time"],
                    week=week,
                )
            created += 1

        except Exception as e:
            typer.echo(f"[ERR] {e}", err=True)
            errors += 1

    typer.echo(f"Resumen: creados={created}, errores={errors}")

# ===================== CREATE ROOM =====================


@app.command("create-room")
def create_room(
    name: str = typer.Option(..., help="Nombre de la sala"),
    description: str = typer.Option("", help="Descripción"),
    player_limit: int = typer.Option(10, help="Límite de jugadores"),
    coins: int = typer.Option(10, help="Costo de entrada (monedas)"),
    permission: str = typer.Option("PUBLIC", help="PUBLIC o PRIVATE"),
    password: Optional[str] = typer.Option(None, help="Password si PRIVATE; si PUBLIC debe ir null"),
    image_url: str = typer.Option(NFL_LOGO_URL, help="URL de imagen (NFL)"),
    prize_type: str = typer.Option("money_fixed", help="Tipo de premio; ej money_fixed"),
    percentage: int = typer.Option(100, help="Porcentaje (si aplica)"),
    fixed_amount: int = typer.Option(100, help="Monto fijo (si aplica)"),
    reward_description: str = typer.Option("Premio $100 al ganador", help="Descripción de premio"),
    top_winners: int = typer.Option(1, help="Cantidad de ganadores"),
    start_week: int = typer.Option(..., help="Semana inicial"),
    end_week: int = typer.Option(..., help="Semana final"),
):
    """
    Crea una sala (POST /rooms/) con TODOS los campos que exige tu Swagger.
    """
    require_env()
    headers = auth_headers()

    # Si es PUBLIC, password debe ser null (None en Python -> JSON null)
    if permission.upper() == "PUBLIC":
        password = None

    payload = {
        "name": name,
        "description": description,
        "player_limit": player_limit,
        "coins": coins,
        "permission": permission.upper(),
        "password": password,
        "image_url": image_url,
        "prize_type": prize_type,
        "percentage": percentage,
        "fixed_amount": fixed_amount,
        "reward_description": reward_description,
        "top_winners": top_winners,
        "league_id": LEAGUE_ID,
        "start_week": start_week,
        "end_week": end_week,
    }

    typer.echo(f"POST /rooms -> {payload}")
    url = f"{BASE_URL}/rooms/"
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        typer.echo(f"[ERR {r.status_code}] {r.text}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Room creada: {r.json()}")

# ===================== RESULTS HELPERS =====================


@app.command("dump-week-template")
def dump_week_template(
    week: int = typer.Option(..., help="Semana a exportar"),
    out_file: str = typer.Option(None, help="Ruta JSON de salida; default: data/week_<n>_results.json"),
):
    """
    Exporta plantilla de resultados para la semana a partir de la DB local.
    """
    matches = load_matches_by_week(week)
    if not matches:
        typer.echo(f"No hay partidos guardados para week={week}.", err=True)
        raise typer.Exit(code=1)

    results = [{"match_id": m["api_id"], "team": ""} for m in matches]
    payload = {"results": results}

    if not out_file:
        os.makedirs("data", exist_ok=True)
        out_file = f"data/week_{week}_results.json"

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    typer.echo(f"Plantilla creada: {out_file}")


@app.command("set-week-results")
def set_week_results(
    week: int = typer.Option(..., help="Semana"),
    file: str = typer.Option(..., help="Archivo JSON con {'results': [{'match_id':..., 'team':'home|away'}, ...]}"),
):
    """
    PATCH /matches/results/?week=<week> para cargar resultados.
    """
    require_env()
    headers = auth_headers()

    with open(file, "r", encoding="utf-8") as f:
        payload = json.load(f)

    url = f"{BASE_URL}{RESULTS_PATH}"
    params = {"week": week}
    r = requests.patch(url, headers=headers, params=params, json=payload, timeout=60)
    if r.status_code not in (200, 201):
        typer.echo(f"[ERR {r.status_code}] {r.text}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Resultados aplicados: {r.json()}")

# ===================== MAIN =====================


if __name__ == "__main__":
    app()