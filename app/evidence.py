import re
import unicodedata
from app.x.base import XPost


def normalize(text: str) -> str:
    if not text:
        return ""
    # Unicode NFKC normalization
    normalized = unicodedata.normalize("NFKC", text)

    # Map curly quotes and apostrophes to straight ones
    for c in ["“", "”", "„", "‟", "«", "»"]:
        normalized = normalized.replace(c, '"')
    for c in ["‘", "’", "‚", "‛", "′", "‵", "`"]:
        normalized = normalized.replace(c, "'")

    # Lowercase and collapse whitespace
    collapsed = re.sub(r"\s+", " ", normalized).strip().lower()
    return collapsed


def quote_is_supported(quote: str, source_text: str, min_len: int = 12) -> bool:
    if not quote or not source_text:
        return False
    # Strip leading/trailing ... or … and whitespace
    cleaned_quote = quote.strip()
    while cleaned_quote.startswith("...") or cleaned_quote.startswith("…"):
        cleaned_quote = cleaned_quote.lstrip(".… ").strip()
    while cleaned_quote.endswith("...") or cleaned_quote.endswith("…"):
        cleaned_quote = cleaned_quote.rstrip(".… ").strip()

    q = normalize(cleaned_quote)
    s = normalize(source_text)
    return len(q) >= min_len and q in s


def validate_evidence(
    items: list[dict],
    bio: str,
    posts_by_id: dict[str, XPost],
    author_id: str,
    username: str,
) -> list[dict]:
    clean_username = username.lstrip("@")
    valid_items = []

    for it in items:
        source = it.get("source")
        quote = it.get("quote", "")
        post_id = it.get("post_id")

        if source == "bio":
            if quote_is_supported(quote, bio):
                res = dict(it)
                res["url"] = f"https://x.com/{clean_username}"
                valid_items.append(res)
        elif source == "post":
            if post_id and str(post_id) in posts_by_id:
                post = posts_by_id[str(post_id)]
                if str(post.author_id) == str(author_id) and quote_is_supported(
                    quote, post.text
                ):
                    res = dict(it)
                    res["post_id"] = str(post_id)
                    res["url"] = f"https://x.com/{clean_username}/status/{post_id}"
                    valid_items.append(res)

    return valid_items
