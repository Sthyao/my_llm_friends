from pathlib import Path


_PROMPT_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """Read a UTF-8 prompt without its final file newline."""
    return (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").removesuffix("\n")
