from datetime import datetime, timezone
import sqlite3
import httpx

from app import db, ledger
from app.clock import iso
from app.crypto import TokenCipher
from app.x.base import (
    XUser,
    XPost,
    XApiError,
    XRateLimited,
    XAuthError,
)

API = "https://api.x.com/2"
POST_FIELDS_PARAM = "post.fields"
USER_FIELDS = "description,protected,location,public_metrics,receives_your_dm,connection_status"
POST_FIELDS = "created_at,author_id"


def _parse_x_timestamp(ts_str: str) -> str:
    if not ts_str:
        return ""
    try:
        clean = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return iso(dt)
    except Exception:
        return ts_str


def _epoch_to_iso(epoch_str: str | None) -> str | None:
    if not epoch_str:
        return None
    try:
        dt = datetime.fromtimestamp(float(epoch_str), tz=timezone.utc)
        return iso(dt)
    except Exception:
        return None


class XClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        sender_account_id: int,
        cipher: TokenCipher,
        settings,
        clock,
        http: httpx.Client | None = None,
        cost_context: dict | None = None,
    ):
        self.conn = conn
        self.sender_account_id = sender_account_id
        self.cipher = cipher
        self.settings = settings
        self.clock = clock
        self.http = http or httpx.Client(timeout=20.0)
        self.cost_context = cost_context or {}

        # Load tokens from DB
        cur = conn.execute(
            "SELECT token_ciphertext FROM sender_accounts WHERE id = ?",
            (sender_account_id,),
        )
        row = cur.fetchone()
        if not row or not row["token_ciphertext"]:
            raise XAuthError(401, "no_tokens", "No tokens found for sender account")

        try:
            self._token_data = self.cipher.decrypt(row["token_ciphertext"])
        except Exception as e:
            raise XAuthError(401, "invalid_tokens", f"Failed to decrypt tokens: {e}") from e

    def set_cost_context(self, campaign_id: int | None, run_id: int | None) -> None:
        self.cost_context = {"campaign_id": campaign_id, "run_id": run_id}

    def _record_cost(self, endpoint: str, count: int) -> None:
        if count <= 0:
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
            simulated=False,
        )

    def _refresh_tokens(self) -> None:
        refresh_token = self._token_data.get("refresh_token")
        if not refresh_token:
            raise XAuthError(401, "no_refresh_token", "No refresh token available")

        url = f"{API}/oauth2/token"
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.settings.x_client_id,
        }
        auth = (self.settings.x_client_id, self.settings.x_client_secret)

        try:
            resp = self.http.post(url, data=data, auth=auth)
        except Exception as e:
            raise XAuthError(401, "refresh_network_error", str(e)) from e

        if resp.status_code != 200:
            raise XAuthError(resp.status_code, "refresh_failed", resp.text)

        new_tokens = resp.json()
        obtained_at = self._token_data.get("obtained_at", iso(self.clock.now()))
        self._token_data.update(new_tokens)
        self._token_data["obtained_at"] = obtained_at

        blob = self.cipher.encrypt(self._token_data)
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE sender_accounts SET token_ciphertext = ? WHERE id = ?",
                (blob, self.sender_account_id),
            )

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_data: dict | None = None,
    ) -> dict:
        url = f"{API}{path}" if path.startswith("/") else f"{API}/{path}"
        headers = {"Authorization": f"Bearer {self._token_data.get('access_token', '')}"}

        resp = self.http.request(
            method, url, params=params, json=json_data, headers=headers
        )

        if resp.status_code == 401:
            # Refresh once and retry
            self._refresh_tokens()
            headers = {"Authorization": f"Bearer {self._token_data.get('access_token', '')}"}
            resp = self.http.request(
                method, url, params=params, json=json_data, headers=headers
            )
            if resp.status_code == 401:
                raise XAuthError(401, "unauthorized", resp.text)

        if resp.status_code == 429:
            reset_at = _epoch_to_iso(resp.headers.get("x-rate-limit-reset"))
            raise XRateLimited(reset_at, detail=resp.text)

        if resp.is_error:
            try:
                prob = resp.json()
                p_type = prob.get("type", "")
                detail = prob.get("detail", str(prob))
            except Exception:
                p_type = ""
                detail = resp.text
            raise XApiError(resp.status_code, p_type, detail)

        return resp.json()

    def _parse_user(self, u: dict) -> XUser:
        metrics = u.get("public_metrics") or {}
        conn_status = tuple(u.get("connection_status") or ())
        return XUser(
            id=str(u["id"]),
            username=u["username"],
            name=u.get("name", ""),
            bio=u.get("description", ""),
            protected=bool(u.get("protected", False)),
            location=u.get("location", "") or "",
            followers_count=metrics.get("followers_count"),
            receives_your_dm=u.get("receives_your_dm"),
            connection_status=conn_status,
        )

    def _parse_post(self, p: dict) -> XPost:
        return XPost(
            id=str(p["id"]),
            author_id=str(p["author_id"]),
            text=p.get("text", ""),
            created_at=_parse_x_timestamp(p.get("created_at", "")),
        )

    def me(self) -> XUser:
        res = self._request("GET", "/users/me", params={"user.fields": USER_FIELDS})
        data = res.get("data")
        if not data:
            raise XApiError(500, "missing_data", "No user data returned from /users/me")
        self._record_cost("users_me", 1)
        return self._parse_user(data)

    def users_by_usernames(self, usernames: list[str]) -> list[XUser]:
        cleaned = [u.lstrip("@").strip() for u in usernames if u.lstrip("@").strip()]
        if not cleaned:
            return []

        users = []
        for i in range(0, len(cleaned), 100):
            chunk = cleaned[i : i + 100]
            params = {
                "usernames": ",".join(chunk),
                "user.fields": USER_FIELDS,
            }
            res = self._request("GET", "/users/by", params=params)
            data = res.get("data") or []
            self._record_cost("users_lookup", len(data))
            for u in data:
                users.append(self._parse_user(u))
        return users

    def user_by_id(self, user_id: str) -> XUser | None:
        res = self._request("GET", f"/users/{user_id}", params={"user.fields": USER_FIELDS})
        data = res.get("data")
        if not data:
            return None
        self._record_cost("users_lookup", 1)
        return self._parse_user(data)

    def send_dm(self, participant_id: str, text: str) -> dict:
        res = self._request(
            "POST",
            f"/dm_conversations/with/{participant_id}/messages",
            json_data={"text": text},
        )
        self._record_cost("dm_send", 1)
        return res.get("data") or {}

    def search_recent(
        self, query: str, max_posts: int
    ) -> tuple[list[XPost], list[XUser]]:
        if max_posts <= 0:
            return [], []

        all_posts: list[XPost] = []
        authors_dict: dict[str, XUser] = {}
        next_token = None

        while len(all_posts) < max_posts:
            page_size = min(100, max(10, max_posts - len(all_posts)))
            params = {
                "query": query,
                "max_results": page_size,
                "expansions": "author_id",
                "user.fields": USER_FIELDS,
                POST_FIELDS_PARAM: POST_FIELDS,
            }
            if next_token:
                params["next_token"] = next_token

            res = self._request("GET", "/tweets/search/recent", params=params)
            data = res.get("data") or []
            includes = res.get("includes") or {}
            users = includes.get("users") or []

            self._record_cost("search_posts", len(data))
            self._record_cost("search_authors", len(users))

            for u in users:
                user_obj = self._parse_user(u)
                authors_dict[user_obj.id] = user_obj

            for p in data:
                all_posts.append(self._parse_post(p))
                if len(all_posts) >= max_posts:
                    break

            meta = res.get("meta") or {}
            next_token = meta.get("next_token")
            if not next_token:
                break

        return all_posts[:max_posts], list(authors_dict.values())

    def following(self, user_id: str, max_users: int) -> list[XUser]:
        if max_users <= 0:
            return []

        all_users: list[XUser] = []
        pagination_token = None

        while len(all_users) < max_users:
            page_size = min(1000, max_users - len(all_users))
            params = {
                "max_results": page_size,
                "user.fields": USER_FIELDS,
            }
            if pagination_token:
                params["pagination_token"] = pagination_token

            res = self._request("GET", f"/users/{user_id}/following", params=params)
            data = res.get("data") or []
            self._record_cost("following", len(data))

            for u in data:
                all_users.append(self._parse_user(u))
                if len(all_users) >= max_users:
                    break

            meta = res.get("meta") or {}
            pagination_token = meta.get("pagination_token")
            if not pagination_token:
                break

        return all_users[:max_users]

    def followers(self, user_id: str, max_users: int) -> list[XUser]:
        if max_users <= 0:
            return []

        all_users: list[XUser] = []
        pagination_token = None

        while len(all_users) < max_users:
            page_size = min(1000, max_users - len(all_users))
            params = {
                "max_results": page_size,
                "user.fields": USER_FIELDS,
            }
            if pagination_token:
                params["pagination_token"] = pagination_token

            res = self._request("GET", f"/users/{user_id}/followers", params=params)
            data = res.get("data") or []
            self._record_cost("followers_owned", len(data))

            for u in data:
                all_users.append(self._parse_user(u))
                if len(all_users) >= max_users:
                    break

            meta = res.get("meta") or {}
            pagination_token = meta.get("pagination_token")
            if not pagination_token:
                break

        return all_users[:max_users]

    def user_posts(self, user_id: str, max_posts: int) -> list[XPost]:
        if max_posts <= 0:
            return []

        all_posts: list[XPost] = []
        pagination_token = None

        while len(all_posts) < max_posts:
            page_size = min(100, max(5, max_posts - len(all_posts)))
            params = {
                "max_results": page_size,
                "exclude": "replies,retweets",
                POST_FIELDS_PARAM: POST_FIELDS,
            }
            if pagination_token:
                params["pagination_token"] = pagination_token

            res = self._request("GET", f"/users/{user_id}/tweets", params=params)
            data = res.get("data") or []
            self._record_cost("user_posts", len(data))

            for p in data:
                all_posts.append(self._parse_post(p))
                if len(all_posts) >= max_posts:
                    break

            meta = res.get("meta") or {}
            pagination_token = meta.get("pagination_token")
            if not pagination_token:
                break

        return all_posts[:max_posts]
