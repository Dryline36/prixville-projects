#!/usr/bin/env python3
"""Serve the Prixville swipe feed and accept one signed vote per wallet per project."""

import json
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PRIXVILLE_DB", ROOT / "votes.sqlite3"))
INDEX = ROOT / "index.html"
HOST = os.environ.get("PRIXVILLE_HOST", "127.0.0.1")
PORT = int(os.environ.get("PRIXVILLE_PORT", "8000"))

# The seven projects already in index.html. Unknown ids are rejected.
PROJECT_IDS = (
    "chronolab",
    "accretion",
    "hoodiens",
    "teddy",
    "motif",
    "prezzies",
    "loopers",
)

EXPLORERS = (
    "https://eth.blockscout.com/api/v2",
    "https://base.blockscout.com/api/v2",
)

# First page only. These are the history endpoints Blockscout actually serves.
HISTORY_PATHS = (
    "/addresses/{address}/transactions",
    "/addresses/{address}/transactions?sort=block_number&order=asc",
    "/addresses/{address}/internal-transactions",
    "/addresses/{address}/token-transfers",
)

VOTE_LOCK = threading.Lock()
CHECK_ERROR = "could not check related wallets"
TIED_ERROR = "tied to a wallet that already voted"
BAD_SIGNATURE = "bad signature"
HOLD_ERROR = "This wallet does not hold a Prixville ticket, Prixville NFT, or a Richards"
HOLD_CHECK_ERROR = "could not check Prixville holdings"

# ERC-721 balanceOf(address). Addresses checked against a public page, not guessed.
# Prixville Ticket: Etherscan token tracker and Blockscout name "Prixville Ticket" (prixxxxx).
# Prixville NFT: OpenSea collection "prixville" (linked from prixville.com); on-chain name "Prix".
# The Richards: OpenSea collection "the-richardss" (project URL prixville.com); Blockscout name "The Richards".
HOLDING_CONTRACTS = (
    "0x391c31b74fb6824ed22a59ee325c0ecaf3bbdcc8",
    "0x22115e975da96f3e3eb33771869d387773bd286c",
    "0xf054daf83ec676d9a1e85ce9d1a6d5a65e8e75c1",
)
ETH_RPCS = (
    "https://ethereum-rpc.publicnode.com",
    "https://eth.llamarpc.com",
    "https://cloudflare-eth.com",
)
BALANCE_OF_SELECTOR = "70a08231"


def vote_message(project_id, direction):
    return f"Prixville vote\nProject: {project_id}\nVote: {direction}"


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS votes (
            address TEXT NOT NULL,
            project_id TEXT NOT NULL,
            direction TEXT NOT NULL CHECK (direction IN ('like', 'dislike')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (address, project_id)
        )
        """
    )
    conn.commit()
    return conn


def normalize_address(value):
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if len(text) != 42 or not text.startswith("0x"):
        return None
    hexpart = text[2:]
    if any(ch not in "0123456789abcdef" for ch in hexpart):
        return None
    return text


def address_of(node):
    if isinstance(node, dict):
        value = node.get("hash")
    elif isinstance(node, str):
        value = node
    else:
        return None
    return normalize_address(value)


class RelatedWalletCheckError(Exception):
    """Blockscout did not return a usable first page. The vote must not count."""


def fetch_history_page(url):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "prixville-vote/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = getattr(response, "status", 200)
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RelatedWalletCheckError(CHECK_ERROR) from exc
    if status != 200:
        raise RelatedWalletCheckError(CHECK_ERROR)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RelatedWalletCheckError(CHECK_ERROR) from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise RelatedWalletCheckError(CHECK_ERROR)
    return data["items"]


def counterparties(items, voter):
    found = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        for side in ("from", "to"):
            other = address_of(item.get(side))
            if other and other != voter:
                found.add(other)
    return found


def first_funder(oldest_items, voter):
    """Earliest incoming transfer on the oldest transactions page, if one is there."""
    incoming = []
    for item in oldest_items:
        if not isinstance(item, dict):
            continue
        recipient = address_of(item.get("to"))
        sender = address_of(item.get("from"))
        block = item.get("block_number")
        if recipient != voter or not sender or sender == voter:
            continue
        if not isinstance(block, int):
            continue
        position = item.get("position")
        if not isinstance(position, int):
            position = 0
        incoming.append((block, position, sender))
    if not incoming:
        return None
    incoming.sort()
    return incoming[0][2]


def related_addresses(voter):
    """Addresses Blockscout shows on the first page of public history.

    Sent-to and received-from counterparties, plus the address that first
    funded this wallet when that incoming transfer is on the oldest page.
    No other links are added.
    """
    related = set()
    for base in EXPLORERS:
        oldest_items = None
        for path in HISTORY_PATHS:
            url = base + path.format(address=voter)
            items = fetch_history_page(url)
            if path.endswith("order=asc"):
                oldest_items = items
            related.update(counterparties(items, voter))
        if oldest_items is not None:
            funder = first_funder(oldest_items, voter)
            if funder:
                related.add(funder)
    related.discard(voter)
    return related



class HoldingsCheckError(Exception):
    """A public Ethereum RPC did not return a usable balanceOf result."""


def erc721_balance(contract, owner):
    data = "0x" + BALANCE_OF_SELECTOR + owner[2:].rjust(64, "0")
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": contract, "data": data}, "latest"],
        }
    ).encode("utf-8")
    last_error = None
    for rpc in ETH_RPCS:
        request = urllib.request.Request(
            rpc,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "prixville-vote/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if getattr(response, "status", 200) != 200:
                    last_error = response.status
                    continue
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_error = exc
            continue
        if not isinstance(body, dict) or body.get("error") or "result" not in body:
            last_error = body
            continue
        result = body["result"]
        if not isinstance(result, str) or not result.startswith("0x") or len(result) < 3:
            last_error = result
            continue
        try:
            return int(result, 16)
        except ValueError as exc:
            last_error = exc
            continue
    raise HoldingsCheckError(HOLD_CHECK_ERROR) from (last_error if isinstance(last_error, Exception) else None)


def holds_prixville_nft(voter):
    """True if balanceOf is at least 1 on any verified Prixville ERC-721."""
    for contract in HOLDING_CONTRACTS:
        if erc721_balance(contract, voter) > 0:
            return True
    return False


def recover_signer(message, signature):
    if not isinstance(signature, str) or not signature.startswith("0x"):
        raise ValueError(BAD_SIGNATURE)
    hexpart = signature[2:]
    if len(hexpart) != 130 or any(ch not in "0123456789abcdefABCDEF" for ch in hexpart):
        raise ValueError(BAD_SIGNATURE)
    try:
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception as exc:
        raise ValueError(BAD_SIGNATURE) from exc
    signer = normalize_address(recovered)
    if not signer:
        raise ValueError(BAD_SIGNATURE)
    return signer


def public_counts(conn):
    counts = {project_id: {"likes": 0, "dislikes": 0} for project_id in PROJECT_IDS}
    rows = conn.execute(
        "SELECT project_id, direction, COUNT(*) FROM votes GROUP BY project_id, direction"
    )
    for project_id, direction, count in rows:
        bucket = counts.get(project_id)
        if not bucket:
            continue
        if direction == "like":
            bucket["likes"] = count
        elif direction == "dislike":
            bucket["dislikes"] = count
    return counts


def cast_vote(conn, address, project_id, direction, signature):
    if project_id not in PROJECT_IDS:
        return 400, {"error": "unknown project"}
    if direction not in ("like", "dislike"):
        return 400, {"error": "vote must be like or dislike"}
    voter = normalize_address(address)
    if not voter:
        return 400, {"error": BAD_SIGNATURE}
    message = vote_message(project_id, direction)
    try:
        signer = recover_signer(message, signature)
    except ValueError:
        return 400, {"error": BAD_SIGNATURE}
    if signer != voter:
        return 400, {"error": BAD_SIGNATURE}

    with VOTE_LOCK:
        already = conn.execute(
            "SELECT 1 FROM votes WHERE address = ? AND project_id = ?",
            (voter, project_id),
        ).fetchone()
        if already:
            return 409, {"error": "this wallet already voted on this project"}
        try:
            if not holds_prixville_nft(voter):
                return 403, {"error": HOLD_ERROR}
        except HoldingsCheckError:
            return 503, {"error": HOLD_CHECK_ERROR}
        try:
            linked = related_addresses(voter)
        except RelatedWalletCheckError:
            return 503, {"error": CHECK_ERROR}
        if linked:
            placeholders = ",".join("?" for _ in linked)
            hit = conn.execute(
                f"SELECT 1 FROM votes WHERE project_id = ? AND address IN ({placeholders}) LIMIT 1",
                (project_id, *sorted(linked)),
            ).fetchone()
            if hit:
                return 409, {"error": TIED_ERROR}
        try:
            conn.execute(
                "INSERT INTO votes (address, project_id, direction) VALUES (?, ?, ?)",
                (voter, project_id, direction),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return 409, {"error": "this wallet already voted on this project"}
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN direction = 'like' THEN 1 ELSE 0 END),
              SUM(CASE WHEN direction = 'dislike' THEN 1 ELSE 0 END)
            FROM votes WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()
    likes, dislikes = row if row else (0, 0)
    return 200, {
        "ok": True,
        "likes": likes or 0,
        "dislikes": dislikes or 0,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "prixville-vote/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _send(self, status, body, content_type):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                page = INDEX.read_bytes()
            except OSError:
                self._send(500, b"index.html missing", "text/plain; charset=utf-8")
                return
            self._send(200, page, "text/html; charset=utf-8")
            return
        if path == "/api/counts":
            self._json(200, public_counts(self.server.db))
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/vote":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > 8192:
            self._json(400, {"error": "bad request"})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "bad request"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "bad request"})
            return
        expected = None
        project_id = payload.get("project_id")
        direction = payload.get("direction")
        if isinstance(project_id, str) and direction in ("like", "dislike"):
            expected = vote_message(project_id, direction)
        message = payload.get("message")
        if expected is not None and message is not None and message != expected:
            self._json(400, {"error": BAD_SIGNATURE})
            return
        status, body = cast_vote(
            self.server.db,
            payload.get("address"),
            project_id,
            direction,
            payload.get("signature"),
        )
        self._json(status, body)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.db = connect()
    print(f"Prixville swipe feed at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping")
    finally:
        server.server_close()
        server.db.close()


if __name__ == "__main__":
    main()
