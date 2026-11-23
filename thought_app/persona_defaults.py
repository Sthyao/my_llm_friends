"""Built-in persona drafts for new Agents and the existing named roles."""

from hashlib import sha256

from exp.harness import persona_source_hash

from .prompts import load_prompt


AGENT_PERSONA_REWRITE_KEY = "agent_persona_rewrite_v1"

_STARTERS = (
    ("keeper", "拾荒者", "friend", False),
    ("critic", "刺头", "friend", False),
    ("dreamer", "造梦师", "friend", False),
    ("positive", "积极", "friend", False),
    ("mirror", "影子自我", "mirror", False),
    ("psychology", "自我分析心理学专家", "friend", True),
    ("social-science", "社会科学分析专家", "friend", True),
)

_PRIOR_REPLY_STYLE_HASHES = {
    "07799177df62df350606dc00849586bdc46c5bec5de3ff404664c20aed9430d7": "keeper",
    "e8101078903fd01394435fe1f00f10da0d7d250f007bf268511119c31298d8da": "critic",
    "7f901e8cfe491bfbae7b6e2ef29ada959ae30f3d94de7d42a00a76820a145568": "dreamer",
    "2f88da9cda0b27a3c95799b0ef1432d18bded33fa878177702fc293d43205eb7": "mirror",
    "4d4daaedf46c7e682c2481b4b6fa68844843a134a743ece7ce5ad706b31ac19b": "psychology",
}


def agent_starters():
    return [
        {
            "id": slug,
            "name": name,
            "prompt": load_prompt(f"personas/{slug}"),
            "role_mode": role_mode,
            "web_search": web_search,
        }
        for slug, name, role_mode, web_search in _STARTERS
    ]


def migrate_builtin_agent_personas(conn):
    """Rewrite the named roles once, keeping bot IDs and model assignments."""
    changed = conn.execute(
        "INSERT INTO config_settings (key, value) VALUES (?, '1') ON CONFLICT DO NOTHING",
        (AGENT_PERSONA_REWRITE_KEY,),
    )
    if not changed.rowcount:
        return

    drafts = {draft["name"]: draft["prompt"] for draft in agent_starters()}
    placeholders = ", ".join("?" for _ in drafts)
    bots = conn.execute(
        f"""
        SELECT b.id, b.name, b.persona_id, p.web_search
        FROM bots b JOIN personas p ON p.id = b.persona_id
        WHERE b.name IN ({placeholders})
        ORDER BY b.id
        """,
        tuple(drafts),
    ).fetchall()

    for bot in bots:
        name = bot["name"]
        draft = drafts[name]
        persona_id = bot["persona_id"]
        shared = conn.execute(
            "SELECT 1 FROM bots WHERE persona_id = ? AND id != ?",
            (persona_id, bot["id"]),
        ).fetchone()
        if shared:
            persona_id = conn.execute(
                "INSERT INTO personas (name, prompt, web_search) VALUES (?, ?, ?)",
                (name, draft, bot["web_search"]),
            ).lastrowid
            conn.execute("UPDATE bots SET persona_id = ? WHERE id = ?", (persona_id, bot["id"]))
        else:
            conn.execute("UPDATE personas SET name = ?, prompt = ? WHERE id = ?", (name, draft, persona_id))


def migrate_builtin_reply_styles(conn):
    """Update untouched default drafts while keeping their evolved skills active."""
    personas = conn.execute("SELECT id, prompt FROM personas").fetchall()
    for persona in personas:
        source = persona["prompt"]
        slug = _PRIOR_REPLY_STYLE_HASHES.get(sha256(source.encode()).hexdigest())
        if slug is None:
            continue
        updated = load_prompt(f"personas/{slug}")
        conn.execute("UPDATE personas SET prompt = ? WHERE id = ?", (updated, persona["id"]))
        conn.execute("""
            UPDATE agent_evolution_traits SET base_hash = ?
            WHERE base_hash = ? AND note_id IN (
                SELECT n.id FROM bot_evolution_notes n
                JOIN bots b ON b.id = n.bot_id WHERE b.persona_id = ?
            )
        """, (persona_source_hash(updated), persona_source_hash(source), persona["id"]))
