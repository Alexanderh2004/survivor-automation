# survivor.py
from __future__ import annotations

import json
import os
import random
import string
from datetime import datetime, timedelta
from typing import List, Optional

import requests
import typer
from dotenv import load_dotenv
from pydantic import BaseModel

app = typer.Typer(add_completion=False)

# ===================== ENV & CONFIG =====================

load_dotenv()

FILENAME = "created_rooms.json"

BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
GAME_ID = os.getenv("GAME_ID", "")
FS_USERNAME = os.getenv("FS_USERNAME", "")
FS_PASSWORD = os.getenv("FS_PASSWORD", "")
API_TOKEN = os.getenv("API_TOKEN", "")

NFL_LOGO_URL = (
    "https://upload.wikimedia.org/wikipedia/en/a/a2/National_Football_League_logo.svg"
)


class Team(BaseModel):
    pk: str
    fields: dict


class League(BaseModel):
    id: str
    weeks: List[List[str]]


class Room(BaseModel):
    id: str
    league_id: str
    start_time_epoch: int
    weeks: List[List[str]]
    finished: bool = False


class MatchResult(BaseModel):
    match_id: str
    team: str


def require_env() -> None:
    missing = []
    if not BASE_URL:
        missing.append("BASE_URL")
    if not (API_TOKEN or (FS_USERNAME and FS_PASSWORD)):
        missing.append("API_TOKEN o FS_USERNAME/FS_PASSWORD")
    if not GAME_ID:
        missing.append("GAME_ID")
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
            "username": FS_USERNAME,
            "password": FS_PASSWORD,
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
        raise RuntimeError(
            f"Login OK pero no encontré token en respuesta: {data}"
        )
    return token


def auth_headers() -> dict:
    token = login_and_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

# ===================== TEAMS / TIME HELPERS =====================


def iso_utc(dt_utc: datetime) -> str:
    # Ejemplo del Swagger incluía microsegundos + 'Z'
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def load_teams() -> List[Team]:
    with open("data/teams.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    return [Team(**team) for team in data]


def create_league(time_diff: timedelta, cnt_league: int) -> League:
    url = f"{BASE_URL}/leagues/"
    payload = {
        "game_id": GAME_ID,
        "short_name": f"NFL-{cnt_league}",
        "name": f"NFL {cnt_league}",
        "badge_url": NFL_LOGO_URL,
    }
    headers = auth_headers()
    res = requests.post(url, headers=headers, json=payload, timeout=30)
    res.raise_for_status()
    res = res.json()
    id = res.get("id")

    teams = load_teams()
    match_url = f"{BASE_URL}/matches/"
    weeks = []
    for week in range(1, 4):
        matchs = []
        for mtch in range(3):
            external_id = "".join(
                random.choices(string.ascii_uppercase + string.digits, k=32)
            )
            body = {
                "external_id": external_id,
                "league_id": id,
                "home_id": teams[2*(week * 3 + mtch)].pk,
                "away_id": teams[2*(week * 3 + mtch) + 1].pk,
                "season": "2025",
                "week": week,
                "start_time": iso_utc(datetime.utcnow() + time_diff),
            }
            res = requests.post(
                match_url, headers=headers, json=body, timeout=30
            )
            try:
                res.raise_for_status()
            except requests.HTTPError as e:
                print(f"Error creating match: {e}")
                print(f"Response: {res.text}")
                raise
            res = res.json()
            match_id = res.get("id")
            matchs.append(match_id)
        weeks.append(matchs)
    return League(id=id, weeks=weeks)

# ===================== CREATE ROOM =====================


def create_room(
    league_id: str,
    name: str,
    description: str,
    player_limit: int,
    coins: int,
    permission: str = "PUBLIC",
    password: Optional[str] = None,
    image_url: str = NFL_LOGO_URL,
    prize_type: str = "money_fixed",
    percentage: int = 100,
    fixed_amount: int = 100,
    reward_description: str = "Premio $100 al ganador",
    top_winners: int = 1,
    start_week: int = ...,
    end_week: int = ...,
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
        "league_id": league_id,
        "start_week": start_week,
        "end_week": end_week,
    }

    url = f"{BASE_URL}/rooms/"
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        typer.echo(f"[ERR {r.status_code}] {r.text}", err=True)
        raise typer.Exit(code=1)
    return r.json()

# ===================== RESULTS HELPERS =====================


def set_week_results(
    week: int,
    results: List[MatchResult]
):
    """
    PATCH /matches/results/?week=<week> para cargar resultados.
    """
    require_env()
    headers = auth_headers()

    url = f"{BASE_URL}/matches/results/"
    params = {"week": week}
    r = requests.patch(
        url, headers=headers, params=params,
        json={"results": [result.dict() for result in results]},
        timeout=60
    )
    if r.status_code not in (200, 201):
        typer.echo(f"[ERR {r.status_code}] {r.text}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Resultados aplicados: {r.json()}")


@app.command("create-all-rooms")
def create_all_rooms():
    cnt = 1
    data = {}

    for week in range(1):
        eps_list = [
            timedelta(minutes=15),
            timedelta(minutes=30),
            timedelta(minutes=45),
            timedelta(hours=1),
        ]
        for eps in eps_list:
            time_diff = timedelta(days=week) + eps
            league = create_league(time_diff, cnt_league=cnt)
            room = create_room(
                league_id=league.id,
                name=f"SURVIVOR NFL {cnt}",
                description=f"Sala NFL {league.id}",
                player_limit=20,
                coins=10,
                permission="PUBLIC",
                password=None,
                image_url=NFL_LOGO_URL,
                prize_type="money_fixed",
                percentage=100,
                fixed_amount=100,
                top_winners=1,
                start_week=1,
                end_week=3,
            )
            unix_time = int((datetime.utcnow() + time_diff).timestamp())
            typer.echo(
                f"Creada sala {room['id']} para liga {league.id} "
                f"con inicio en {unix_time} (epoch)"
            )
            data[room['id']] = Room(
                id=room['id'],
                league_id=league.id,
                start_time_epoch=unix_time,
                weeks=league.weeks,
            ).dict()
            cnt += 1
    with open(FILENAME, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


@app.command("set-results")
def set_results():
    with open(FILENAME, "r", encoding="utf-8") as f:
        created_rooms = json.load(f)
    for id, room_dict in created_rooms.items():
        room = Room(**room_dict)
        if room.finished:
            typer.echo(f"Room {id} already finished, skipping.")
            continue
        an_hour_ago = datetime.utcnow() - timedelta(hours=1)
        if room.start_time_epoch > int(an_hour_ago.timestamp()):
            typer.echo(f"Room {id} not finished yet, skipping.")
            continue
        for i, week in enumerate(room.weeks):
            set_week_results(
                week=i + 1,
                results=[MatchResult(match_id=m, team="home") for m in week],
            )
        created_rooms[id]['finished'] = True
    with open(FILENAME, "w", encoding="utf-8") as f:
        json.dump(created_rooms, f, indent=2)

# ===================== MAIN =====================


if __name__ == "__main__":
    app()
# survivor.py
