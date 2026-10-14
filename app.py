import os
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import timezone

import psycopg2
from psycopg2.extras import RealDictCursor, register_uuid
from flask import Flask, g, jsonify, redirect, request, send_file

register_uuid()

app = Flask(__name__)

COOKIE_NAME = "boogle_id"               # name it is given in storage
COOKIE_MAX_AGE = 60 * 60 * 24 * 365     # basically expiration date
HTML_PATH = os.path.join(os.path.dirname(__file__), "boogle.html")


@contextmanager
def database_connection():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL must be set to connect to PostgreSQL")

    connection = psycopg2.connect(database_url)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def serialize_timestamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def get_cookie_client_id():
    value = request.cookies.get(COOKIE_NAME)
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


def record_visit(connection, client_id):
    ip_address = request.remote_addr
    with connection.cursor() as cursor:
        if client_id is not None and ip_address is not None:
            cursor.execute(
                """
                INSERT INTO clients (client_id, latest_ip, ips)
                VALUES (%s, %s, ARRAY[%s]::INET[])
                ON CONFLICT (client_id) DO UPDATE
                SET last_seen = now(),
                    latest_ip = EXCLUDED.latest_ip,
                    ips = CASE
                        WHEN EXCLUDED.latest_ip = ANY(clients.ips)
                        THEN clients.ips
                        ELSE array_append(clients.ips, EXCLUDED.latest_ip)
                    END
                """,
                (client_id, ip_address, ip_address),
            )
        elif client_id is not None:
            cursor.execute(
                """
                INSERT INTO clients (client_id)
                VALUES (%s)
                ON CONFLICT (client_id) DO UPDATE SET last_seen = now()
                """,
                (client_id,),
            )

        cursor.execute(
            """
            INSERT INTO requests (client_id, ip, referer, path)
            VALUES (%s, %s, %s, %s)
            RETURNING request_id
            """,
            (client_id, ip_address, request.referrer, request.path),
        )
        return cursor.fetchone()[0]


@app.before_request
def track_request():
    client_id = get_cookie_client_id()
    g.new_client_cookie = None

    if request.path == "/" and not request.cookies.get(COOKIE_NAME):
        client_id = uuid.uuid4()
        g.new_client_cookie = str(client_id)

    g.client_id = client_id
    with database_connection() as connection:
        g.request_id = record_visit(connection, client_id)


@app.after_request
def set_client_cookie(response):
    client_id = getattr(g, "new_client_cookie", None)
    if client_id is not None:
        response.set_cookie(
            COOKIE_NAME,
            client_id,
            max_age=COOKIE_MAX_AGE,
            path="/",
            samesite="Lax",
        )
    return response


@app.get('/')
def index():
    return send_file(HTML_PATH)


@app.get('/jsontest')
def jsontest():
    return jsonify({"data": "I am in CSE190/CSE291!"}), 200


@app.get('/search')
def search():
    query = request.args.get("q", "")
    if query.strip():
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO searches (client_id, request_id, query)
                    VALUES (%s, %s, %s)
                    """,
                    (g.client_id, g.request_id, query),
                )
    return redirect("/", code=303)


@app.get('/api/history')
def history():
    client_id = g.client_id
    searches = []
    if client_id is not None:
        with database_connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT query, ts
                    FROM searches
                    WHERE client_id = %s
                    ORDER BY ts DESC, search_id DESC
                    """,
                    (client_id,),
                )
                searches = [
                    {"query": row["query"], "ts": serialize_timestamp(row["ts"])}
                    for row in cursor.fetchall()
                ]

    return jsonify(
        {"client_id": str(client_id) if client_id else None, "searches": searches}
    )


@app.get('/dump')
def dump():
    with database_connection() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT client_id, first_seen, last_seen, latest_ip, ips
                FROM clients
                ORDER BY first_seen, client_id
                """
            )
            clients = cursor.fetchall()

            cursor.execute(
                """
                SELECT client_id, query, ts
                FROM searches
                WHERE client_id IS NOT NULL
                ORDER BY ts DESC, search_id DESC
                """
            )
            searches_by_client = defaultdict(list)
            for row in cursor.fetchall():
                searches_by_client[row["client_id"]].append(
                    {"query": row["query"], "ts": serialize_timestamp(row["ts"])}
                )

    return jsonify(
        {
            "clients": [
                {
                    "client_id": str(client["client_id"]),
                    "first_seen": serialize_timestamp(client["first_seen"]),
                    "last_seen": serialize_timestamp(client["last_seen"]),
                    "latest_ip": (
                        str(client["latest_ip"])
                        if client["latest_ip"] is not None
                        else None
                    ),
                    "ips": [str(ip) for ip in client["ips"] or []],
                    "searches": searches_by_client[client["client_id"]],
                }
                for client in clients
            ]
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port="8000")
