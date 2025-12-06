import base64
import socketio
import uvicorn
from fastapi import FastAPI, responses
from fastapi.middleware.cors import CORSMiddleware
import os
import dotenv
import random
import utils
import uuid
from typing import Callable, List, Optional
from enum import Enum
import logging

# ------------------ Logging ------------------
logging.getLogger("uvicorn").setLevel(logging.CRITICAL)
logging.getLogger("uvicorn.access").setLevel(logging.CRITICAL)

# ------------------ Global state ------------------
lobbies = {}
player_to_lobby = {}
sid_to_id = {}
id_to_sid = {}

# ------------------ Utilities ------------------
def get_id_from_sid(sid: str) -> str:
    return sid_to_id.get(sid, sid)

def get_sid_from_id(player_id: str) -> str:
    return id_to_sid.get(player_id, player_id)

def event_print(colour: utils.Colors, event: str, *args):
    print(f"{colour}[{event}]{utils.Colors.END}", *args)

def get_lobby_and_player(player_id: str):
    lobby_code = player_to_lobby.get(player_id)
    if not lobby_code:
        return None, None
    lobby = lobbies.get(lobby_code)
    return lobby, lobby_code

# ------------------ Enums ------------------
class GameState(Enum):
    WAITING = "waiting"
    WRITING = "writing"
    VIEWING = "viewing"
    DRAWING = "drawing"
    PRESENTING = "presenting"
    END = "end"

# ------------------ Player & Lobby ------------------
class Player:
    def __init__(self, id: str, username: str):
        self.id = id
        self.username = username
        self.prompt_written: Optional[str] = None
        self.prompt_given: Optional[str] = None
        self.drawing_data: Optional[bytes] = None
        self.voted_already: bool = False
        self.voting_score: int = 0

    def get_as_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "prompt_written": self.prompt_written,
            "prompt_given": self.prompt_given
        }

class Lobby:
    def __init__(self, host_id: str):
        self.host_id = host_id
        self.lobby_code = self._generate_lobby_code()
        self.players: List[Player] = []
        self.current_state = GameState.WAITING

    @staticmethod
    def _generate_lobby_code():
        while True:
            code = random.randint(1000, 999999)
            if str(code) not in lobbies:
                return code

    async def add_player(self, player: Player, callback: Optional[Callable] = None):
        self.players.append(player)
        if callback:
            await callback(player)

    async def remove_player(self, player_id: str, callback: Optional[Callable] = None):
        player = next((p for p in self.players if p.id == player_id), None)
        if player:
            self.players.remove(player)
            if callback:
                await callback(player)

# ------------------ FastAPI + CORS ------------------
dotenv.load_dotenv()
app = FastAPI(docs_url="/docs")

origins = ["*.latific.click"]
if os.getenv("INSECURE_CORS", "0").lower() in ("1", "true", "yes"):
    origins = "*"
    event_print(utils.Colors.RED, "CORS", "USING INSECURE CORS!")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------ Socket.IO ------------------
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=origins, ping_timeout=25, ping_interval=8)
asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)

# ------------------ Matchmaking & Connection ------------------
@sio.event
async def connect(sid, environ):
    event_print(utils.Colors.GREEN, "CONNECT", f"New connection: {sid}")

@sio.event
async def assign_id(sid, data):
    player_id = data or uuid.uuid4().hex
    sid_to_id[sid] = player_id
    id_to_sid[player_id] = sid
    event_print(utils.Colors.GREEN, "ASSIGN_ID", f"Assigned player_id: {player_id}")
    await sio.emit("assign_id", player_id, sid)

@sio.event
async def disconnect(sid):
    player_id = sid_to_id.pop(sid, None)
    if not player_id:
        return
    lobby, lobby_code = get_lobby_and_player(player_id)
    id_to_sid.pop(player_id, None)
    player_to_lobby.pop(player_id, None)
    if not lobby:
        return
    if lobby.host_id == player_id:
        for p in lobby.players:
            if p.id != player_id:
                sid_ = id_to_sid.pop(p.id, None)
                player_to_lobby.pop(p.id, None)
                sid_to_id.pop(sid_, None)
                if sid_:
                    await sio.emit("disconnected", "The host has disconnected from the game!", sid_)
        del lobbies[lobby_code]
    else:
        await lobby.remove_player(player_id)
        players = [p.get_as_dict() for p in lobby.players]
        host_sid = id_to_sid.get(lobby.host_id)
        if host_sid:
            await sio.emit("players_update", players, host_sid)
        for p in lobby.players:
            sid_ = id_to_sid.get(p.id)
            if sid_ and sid_ != host_sid:
                await sio.emit("players_update", players, sid_)

# ------------------ Lobby Events ------------------
@sio.event
async def create_lobby(sid, data):
    host_id = get_id_from_sid(sid)
    lobby = Lobby(host_id)
    lobbies[lobby.lobby_code] = lobby
    player_to_lobby[host_id] = lobby.lobby_code
    await sio.emit("lobby_created", lobby.lobby_code, sid)

@sio.event
async def join_lobby(sid, data):
    username = data.get("username")
    lobby_code = data.get("lobby_code")
    if not username or not lobby_code:
        await sio.emit("error", "Username and lobby_code are required!", sid)
        return
    lobby: Lobby = lobbies.get(lobby_code)
    if not lobby:
        await sio.emit("error", "Lobby not found!", sid)
        return
    if lobby.current_state != GameState.WAITING:
        return
    player = Player(get_id_from_sid(sid), username)
    player_to_lobby[player.id] = lobby.lobby_code
    async def on_join(p):
        await sio.emit("joined_lobby", lobby.lobby_code, sid)
        players = [pl.get_as_dict() for pl in lobby.players]
        host_sid = id_to_sid.get(lobby.host_id)
        if host_sid:
            await sio.emit("players_update", players, host_sid)
    await lobby.add_player(player, on_join)

@sio.event
async def cancel_game(sid, data):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    for player in lobby.players:
        await sio.emit("cancel_game", data, get_sid_from_id(player.id))

# ------------------ Game Flow ------------------
@sio.event
async def start_game(sid, data):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    lobby.current_state = GameState.WRITING
    for p in lobby.players:
        await sio.emit("game_state", GameState.WRITING.value, get_sid_from_id(p.id))

@sio.event
async def submit_prompt(sid, data):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    for p in lobby.players:
        if p.id == player_id:
            p.prompt_written = data['prompt']
            await sio.emit("writing_submitted", to=get_sid_from_id(player_id))
    if all(p.prompt_written for p in lobby.players):
        await sio.emit("finish_writing", len(lobby.players), get_sid_from_id(lobby.host_id))

@sio.event
async def hand_out_prompts(sid):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    players = lobby.players
    prompt_pool = [{"id": uuid.uuid4(), "text": p.prompt_written, "owner": p.id} for p in players]
    while True:
        random.shuffle(prompt_pool)
        if all(players[i].id != prompt_pool[i]["owner"] for i in range(len(players))):
            break
    for i, player in enumerate(players):
        player.prompt_given = prompt_pool[i]["text"]
        await sio.emit("give_prompt", player.prompt_given, get_sid_from_id(player.id))
    await sio.emit("start_viewing", "", sid)

@sio.event
async def start_drawing(sid):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    await sio.emit("drawing_started", GameState.DRAWING.value, sid)
    for p in lobby.players:
        await sio.emit("game_state", GameState.DRAWING.value, get_sid_from_id(p.id))

@sio.event
async def submit_drawing(sid, array_buffer, meta=None):
    meta = meta or {}
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    for p in lobby.players:
        if p.id == player_id:
            p.drawing_data = array_buffer

@sio.event
async def end_drawing(sid):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    for p in lobby.players:
        await sio.emit("end_drawing", None, get_sid_from_id(p.id))

@sio.event
async def start_presenting(sid):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    player_data = {
        p.id: {
            "username": p.username,
            "prompt": p.prompt_given,
            "drawing_data": base64.b64encode(p.drawing_data).decode("utf-8") if p.drawing_data else None
        }
        for p in lobby.players
    }
    await sio.emit("player_data", player_data, sid)

@sio.event
async def set_presenter(sid, data):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    for p in lobby.players:
        p.voted_already = False
        if data['id'] == p.id:
            await sio.emit("you_are_presenting", data, get_sid_from_id(p.id))
        else:
            await sio.emit("current_presenter", data, get_sid_from_id(p.id))

@sio.event
async def vote_presentation(sid, data):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    for p in lobby.players:
        if p.id == player_id and data['id'] != p.id and not p.voted_already:
            p.voted_already = True
            for target in lobby.players:
                if target.id == data['id']:
                    target.voting_score += data['score']
                    break
            break
    await sio.emit("voted", None, sid)

@sio.event
async def send_reaction(sid, reaction):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    await sio.emit("reaction", reaction, get_sid_from_id(lobby.host_id))

# ------------------ Root ------------------
@app.api_route("/")
def root():
    greetings = [
        "Hey there!", "Howdy", "Yo!",
        "Ayo!", "Greetings",
    ]
    return responses.PlainTextResponse(random.choice(greetings) + " you know this is just a Socket.IO server, there is no actual web to this?")

# ------------------ Main ------------------
if __name__ == "__main__":
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as temp_sock:
            temp_sock.connect(("8.8.8.8", 80))
            lan_ip = temp_sock.getsockname()[0]
    except Exception:
        lan_ip = None

    port = int(os.environ.get("PORT", 8000))
    event_print(utils.Colors.GREEN, "MAIN", f"Starting webserver on 0.0.0.0:{port}")
    if lan_ip:
        event_print(utils.Colors.GREEN, "MAIN", f"LAN IP: {lan_ip}:{port}")
    uvicorn.run(asgi_app, host="0.0.0.0", port=port, log_level="critical")
import base64
import socketio
import uvicorn
from fastapi import FastAPI, responses
from fastapi.middleware.cors import CORSMiddleware
import os
import dotenv
import random
import utils
import uuid
from typing import Callable, List, Optional
from enum import Enum
import logging

# ------------------ Logging ------------------
logging.getLogger("uvicorn").setLevel(logging.CRITICAL)
logging.getLogger("uvicorn.access").setLevel(logging.CRITICAL)

# ------------------ Global state ------------------
lobbies = {}
player_to_lobby = {}
sid_to_id = {}
id_to_sid = {}

# ------------------ Utilities ------------------
def get_id_from_sid(sid: str) -> str:
    return sid_to_id.get(sid, sid)

def get_sid_from_id(player_id: str) -> str:
    return id_to_sid.get(player_id, player_id)

def event_print(colour: utils.Colors, event: str, *args):
    print(f"{colour}[{event}]{utils.Colors.END}", *args)

def get_lobby_and_player(player_id: str):
    lobby_code = player_to_lobby.get(player_id)
    if not lobby_code:
        return None, None
    lobby = lobbies.get(lobby_code)
    return lobby, lobby_code

# ------------------ Enums ------------------
class GameState(Enum):
    WAITING = "waiting"
    WRITING = "writing"
    VIEWING = "viewing"
    DRAWING = "drawing"
    PRESENTING = "presenting"
    END = "end"

# ------------------ Player & Lobby ------------------
class Player:
    def __init__(self, id: str, username: str):
        self.id = id
        self.username = username
        self.prompt_written: Optional[str] = None
        self.prompt_given: Optional[str] = None
        self.drawing_data: Optional[bytes] = None
        self.voted_already: bool = False
        self.voting_score: int = 0

    def get_as_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "prompt_written": self.prompt_written,
            "prompt_given": self.prompt_given
        }

class Lobby:
    def __init__(self, host_id: str):
        self.host_id = host_id
        self.lobby_code = self._generate_lobby_code()
        self.players: List[Player] = []
        self.current_state = GameState.WAITING

    @staticmethod
    def _generate_lobby_code():
        while True:
            code = random.randint(1000, 999999)
            if str(code) not in lobbies:
                return code

    async def add_player(self, player: Player, callback: Optional[Callable] = None):
        self.players.append(player)
        if callback:
            await callback(player)

    async def remove_player(self, player_id: str, callback: Optional[Callable] = None):
        player = next((p for p in self.players if p.id == player_id), None)
        if player:
            self.players.remove(player)
            if callback:
                await callback(player)

# ------------------ FastAPI + CORS ------------------
dotenv.load_dotenv()
app = FastAPI(docs_url="/docs")

origins = ["*.latific.click"]
if os.getenv("INSECURE_CORS", "0").lower() in ("1", "true", "yes"):
    origins = "*"
    event_print(utils.Colors.RED, "CORS", "USING INSECURE CORS!")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------ Socket.IO ------------------
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=origins, ping_timeout=8, ping_interval=5)
asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)

# ------------------ Matchmaking & Connection ------------------
@sio.event
async def connect(sid, environ):
    event_print(utils.Colors.GREEN, "CONNECT", f"New connection: {sid}")

@sio.event
async def assign_id(sid, data):
    player_id = data or uuid.uuid4().hex
    sid_to_id[sid] = player_id
    id_to_sid[player_id] = sid
    event_print(utils.Colors.GREEN, "ASSIGN_ID", f"Assigned player_id: {player_id}")
    await sio.emit("assign_id", player_id, sid)

@sio.event
async def disconnect(sid):
    player_id = sid_to_id.pop(sid, None)
    if not player_id:
        return
    lobby, lobby_code = get_lobby_and_player(player_id)
    id_to_sid.pop(player_id, None)
    player_to_lobby.pop(player_id, None)
    if not lobby:
        return
    if lobby.host_id == player_id:
        for p in lobby.players:
            if p.id != player_id:
                sid_ = id_to_sid.pop(p.id, None)
                player_to_lobby.pop(p.id, None)
                sid_to_id.pop(sid_, None)
                if sid_:
                    await sio.emit("disconnected", "The host has disconnected from the game!", sid_)
        del lobbies[lobby_code]
    else:
        await lobby.remove_player(player_id)
        players = [p.get_as_dict() for p in lobby.players]
        host_sid = id_to_sid.get(lobby.host_id)
        if host_sid:
            await sio.emit("players_update", players, host_sid)
        for p in lobby.players:
            sid_ = id_to_sid.get(p.id)
            if sid_ and sid_ != host_sid:
                await sio.emit("players_update", players, sid_)

# ------------------ Lobby Events ------------------
@sio.event
async def create_lobby(sid, data):
    host_id = get_id_from_sid(sid)
    lobby = Lobby(host_id)
    lobbies[lobby.lobby_code] = lobby
    player_to_lobby[host_id] = lobby.lobby_code
    await sio.emit("lobby_created", lobby.lobby_code, sid)

@sio.event
async def join_lobby(sid, data):
    username = data.get("username")
    lobby_code = data.get("lobby_code")
    if not username or not lobby_code:
        await sio.emit("error", "Username and lobby_code are required!", sid)
        return
    lobby: Lobby = lobbies.get(lobby_code)
    if not lobby:
        await sio.emit("error", "Lobby not found!", sid)
        return
    if lobby.current_state != GameState.WAITING:
        await sio.emit("error", "Game is already started!", sid)
        return
    if len(lobby.players) >= 12:
        await sio.emit("error", "Lobby is full!", sid)
        return
    player = Player(get_id_from_sid(sid), username)
    player_to_lobby[player.id] = lobby.lobby_code
    async def on_join(p):
        await sio.emit("joined_lobby", lobby.lobby_code, sid)
        players = [pl.get_as_dict() for pl in lobby.players]
        host_sid = id_to_sid.get(lobby.host_id)
        if host_sid:
            await sio.emit("players_update", players, host_sid)
    await lobby.add_player(player, on_join)

@sio.event
async def cancel_game(sid, data):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    for player in lobby.players:
        await sio.emit("cancel_game", data, get_sid_from_id(player.id))

# ------------------ Game Flow ------------------
@sio.event
async def start_game(sid, data):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    lobby.current_state = GameState.WRITING
    for p in lobby.players:
        await sio.emit("game_state", GameState.WRITING.value, get_sid_from_id(p.id))

@sio.event
async def submit_prompt(sid, data):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    for p in lobby.players:
        if p.id == player_id:
            p.prompt_written = data['prompt']
            await sio.emit("writing_submitted", to=get_sid_from_id(player_id))
    if all(p.prompt_written for p in lobby.players):
        await sio.emit("finish_writing", len(lobby.players), get_sid_from_id(lobby.host_id))

@sio.event
async def hand_out_prompts(sid):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    players = lobby.players
    prompt_pool = [{"id": uuid.uuid4(), "text": p.prompt_written, "owner": p.id} for p in players]
    while True:
        random.shuffle(prompt_pool)
        if all(players[i].id != prompt_pool[i]["owner"] for i in range(len(players))):
            break
    for i, player in enumerate(players):
        player.prompt_given = prompt_pool[i]["text"]
        await sio.emit("give_prompt", player.prompt_given, get_sid_from_id(player.id))
    await sio.emit("start_viewing", "", sid)

@sio.event
async def start_drawing(sid):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    await sio.emit("drawing_started", GameState.DRAWING.value, sid)
    for p in lobby.players:
        await sio.emit("game_state", GameState.DRAWING.value, get_sid_from_id(p.id))

@sio.event
async def submit_drawing(sid, array_buffer, meta=None):
    meta = meta or {}
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    for p in lobby.players:
        if p.id == player_id:
            p.drawing_data = array_buffer

@sio.event
async def end_drawing(sid):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    for p in lobby.players:
        await sio.emit("end_drawing", None, get_sid_from_id(p.id))

@sio.event
async def start_presenting(sid):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    player_data = {
        p.id: {
            "username": p.username,
            "prompt": p.prompt_given,
            "drawing_data": base64.b64encode(p.drawing_data).decode("utf-8") if p.drawing_data else None
        }
        for p in lobby.players
    }
    await sio.emit("player_data", player_data, sid)

@sio.event
async def set_presenter(sid, data):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    for p in lobby.players:
        p.voted_already = False
        if data['id'] == p.id:
            await sio.emit("you_are_presenting", data, get_sid_from_id(p.id))
        else:
            await sio.emit("current_presenter", data, get_sid_from_id(p.id))

@sio.event
async def vote_presentation(sid, data):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    for p in lobby.players:
        if p.id == player_id and data['id'] != p.id and not p.voted_already:
            p.voted_already = True
            for target in lobby.players:
                if target.id == data['id']:
                    target.voting_score += data['score']
                    break
            break
    await sio.emit("voted", None, sid)

@sio.event
async def done_presenting(sid):
    player_id = get_id_from_sid(sid)
    lobby, _ = get_lobby_and_player(player_id)
    if not lobby:
        return
    player_stats = []
    for p in lobby.players:
        p: Player
        player_stats.append({
            "username": p.username,
            "score": p.voting_score
        })
    sio.emit("game_end", player_stats, sid)

@sio.event
async def send_reaction(sid, reaction):
    player_id = get_id_from_sid(sid)
    lobby, player = get_lobby_and_player(player_id)
    if not lobby:
        return
    await sio.emit("reaction",{"emoji": reaction, "username": player.username}, get_sid_from_id(lobby.host_id))

# ------------------ Root ------------------
@app.api_route("/")
def root():
    greetings = [
        "Hey there!", "Howdy", "Yo!",
        "Ayo!", "Greetings",
    ]
    return responses.PlainTextResponse(random.choice(greetings) + " you know this is just a Socket.IO server, there is no actual web to this?")

# ------------------ Main ------------------
if __name__ == "__main__":
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as temp_sock:
            temp_sock.connect(("8.8.8.8", 80))
            lan_ip = temp_sock.getsockname()[0]
    except Exception:
        lan_ip = None

    port = int(os.environ.get("PORT", 8000))
    event_print(utils.Colors.GREEN, "MAIN", f"Starting webserver on 0.0.0.0:{port}")
    if lan_ip:
        event_print(utils.Colors.GREEN, "MAIN", f"LAN IP: {lan_ip}:{port}")
    uvicorn.run(asgi_app, host="0.0.0.0", port=port, log_level="critical")
