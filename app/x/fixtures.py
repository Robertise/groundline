from datetime import timedelta
import json
import re
import sqlite3

from app import ledger
from app.clock import iso
from app.x.base import XUser, XPost


class FixtureReader:
    def __init__(
        self,
        path: str,
        clock,
        conn: sqlite3.Connection | None = None,
        cost_context: dict | None = None,
    ):
        self.path = path
        self.clock = clock
        self.conn = conn
        self.cost_context = cost_context or {}

        with open(path, "r", encoding="utf-8") as f:
            self._data = json.load(f)

        self._users_by_id: dict[str, dict] = {
            u["id"]: u for u in self._data.get("users", [])
        }
        self._users_by_username: dict[str, dict] = {
            u["username"].lower(): u for u in self._data.get("users", [])
        }
        sender = self._data.get("sender")
        if sender:
            self._users_by_id[sender["id"]] = sender
            self._users_by_username[sender["username"].lower()] = sender

    def set_cost_context(self, campaign_id: int | None, run_id: int | None) -> None:
        self.cost_context = {"campaign_id": campaign_id, "run_id": run_id}

    def _record_cost(self, endpoint: str, count: int) -> None:
        if not self.conn or count <= 0:
            return
        now_str = iso(self.clock.now())
        campaign_id = self.cost_context.get("campaign_id")
        run_id = self.cost_context.get("run_id")
        ledger.record(
            self.conn,
            endpoint,
            count,
            now_str,
            campaign_id=campaign_id,
            run_id=run_id,
            simulated=True,
        )

    def _make_xuser(self, u: dict) -> XUser:
        return XUser(
            id=str(u["id"]),
            username=u["username"],
            name=u.get("name", ""),
            bio=u.get("bio", ""),
            protected=bool(u.get("protected", False)),
            location=u.get("location", ""),
            followers_count=u.get("followers_count"),
            receives_your_dm=u.get("receives_your_dm"),
            connection_status=tuple(u.get("connection_status") or ()),
        )

    def _make_xpost(self, p: dict) -> XPost:
        days_ago = p.get("days_ago", 0)
        dt = self.clock.now() - timedelta(days=days_ago)
        return XPost(
            id=str(p["id"]),
            author_id=str(p["author_id"]),
            text=p["text"],
            created_at=iso(dt),
        )

    def me(self) -> XUser:
        sender_data = self._data["sender"]
        self._record_cost("users_me", 1)
        return self._make_xuser(sender_data)

    def users_by_usernames(self, usernames: list[str]) -> list[XUser]:
        results = []
        for raw in usernames:
            uname = raw.lstrip("@").strip().lower()
            if uname in self._users_by_username:
                results.append(self._make_xuser(self._users_by_username[uname]))
        self._record_cost("users_lookup", len(results))
        return results

    def search_recent(
        self, query: str, max_posts: int
    ) -> tuple[list[XPost], list[XUser]]:
        # Strip operators: -is:retweet, -is:reply, OR, AND, parens, quotes
        cleaned_q = re.sub(r"-?is:\w+", " ", query)
        cleaned_q = re.sub(r"\b(OR|AND)\b", " ", cleaned_q)
        tokens = [
            t.strip(' "\'(),').lower()
            for t in cleaned_q.split()
            if t.strip(' "\'(),')
        ]

        matched_raw = []
        for p in self._data.get("posts", []):
            text_lower = p["text"].lower()
            if any(tok in text_lower for tok in tokens):
                matched_raw.append(p)

        # Newest first (smaller days_ago is newer)
        matched_raw.sort(key=lambda p: p.get("days_ago", 0))
        sliced = matched_raw[:max_posts]

        posts = [self._make_xpost(p) for p in sliced]
        author_ids = {p.author_id for p in posts}
        authors = [
            self._make_xuser(self._users_by_id[aid])
            for aid in author_ids
            if aid in self._users_by_id
        ]

        self._record_cost("search_posts", len(posts))
        self._record_cost("search_authors", len(authors))

        return posts, authors

    def following(self, user_id: str, max_users: int) -> list[XUser]:
        u = self._users_by_id.get(user_id)
        if not u:
            return []
        username = u["username"]
        following_ids = self._data.get("following", {}).get(username, [])
        sliced_ids = following_ids[:max_users]
        results = [
            self._make_xuser(self._users_by_id[fid])
            for fid in sliced_ids
            if fid in self._users_by_id
        ]
        self._record_cost("following", len(results))
        return results

    def followers(self, user_id: str, max_users: int) -> list[XUser]:
        follower_ids = self._data.get("followers_of_sender", [])
        sliced_ids = follower_ids[:max_users]
        results = [
            self._make_xuser(self._users_by_id[fid])
            for fid in sliced_ids
            if fid in self._users_by_id
        ]
        self._record_cost("followers_owned", len(results))
        return results

    def user_posts(self, user_id: str, max_posts: int) -> list[XPost]:
        user_raw = [
            p for p in self._data.get("posts", []) if str(p["author_id"]) == str(user_id)
        ]
        user_raw.sort(key=lambda p: p.get("days_ago", 0))
        sliced = user_raw[:max_posts]
        posts = [self._make_xpost(p) for p in sliced]
        self._record_cost("user_posts", len(posts))
        return posts
