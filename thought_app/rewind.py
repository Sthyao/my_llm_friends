from fastapi import HTTPException

from .evolution import rewind_evolution
from .journal import get_summary


def rewind_history(db, session_id: int, message_id: int) -> dict:
    """Remove a user message and its suffix within the caller's transaction."""
    cutoff = (session_id, message_id)
    target = db.execute(
        "SELECT role, content FROM messages WHERE session_id = ? AND id = ?", cutoff,
    ).fetchone()
    if target is None:
        raise HTTPException(404, "这条消息不在当前会话中。")
    if target["role"] != "user":
        raise HTTPException(422, "请从一条自己的消息开始回退。")

    restored = rewind_evolution(db, session_id, message_id)
    summary = get_summary(db, session_id)
    summary_removed = bool(summary and (
        summary["through_message_id"] >= message_id or any(
            source_id >= message_id
            for field in ("views", "observations")
            for point in summary[field]
            for source_id in point["source_ids"]
        )
    ))
    if summary_removed:
        db.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
    removed = db.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ? AND id >= ?", cutoff,
    ).fetchone()[0]
    db.execute("DELETE FROM messages WHERE session_id = ? AND id >= ?", cutoff)
    return {
        "draft": target["content"],
        "removed_count": removed,
        "summary_removed": summary_removed,
        "restored_note_count": restored,
    }
