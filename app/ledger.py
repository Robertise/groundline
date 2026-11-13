import math
import sqlite3
from app import db

PRICES_USD = {
    "users_me": 0.001,  # owned read
    "users_lookup": 0.010,  # per user returned
    "search_posts": 0.005,  # per post returned
    "search_authors": 0.010,  # per expanded author; billing of expansions is unconfirmed, so counted
    "following": 0.010,  # per user returned
    "followers_owned": 0.001,  # sender's own followers with the sender's token
    "user_posts": 0.005,  # per post returned
}


class BudgetExceeded(Exception):
    pass


def record(
    conn: sqlite3.Connection,
    endpoint: str,
    count: int,
    now: str,
    campaign_id: int | None = None,
    run_id: int | None = None,
    simulated: bool = False,
    unit_cost: float | None = None,
    detail: str | None = None,
) -> float:
    unit = unit_cost if unit_cost is not None else PRICES_USD.get(endpoint, 0.0)
    cost = round(count * unit, 6)
    sim_flag = 1 if simulated else 0

    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO ledger (
                campaign_id, endpoint, resource_count, unit_cost_usd,
                cost_usd, created_at, run_id, detail, simulated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                endpoint,
                count,
                unit,
                cost,
                now,
                run_id,
                detail,
                sim_flag,
            ),
        )
    return cost


def spent(
    conn: sqlite3.Connection, campaign_id: int, include_simulated: bool = False
) -> float:
    if include_simulated:
        cur = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM ledger WHERE campaign_id = ?",
            (campaign_id,),
        )
    else:
        cur = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM ledger WHERE campaign_id = ? AND simulated = 0",
            (campaign_id,),
        )
    return float(cur.fetchone()[0])


def ensure_budget(
    conn: sqlite3.Connection,
    campaign_id: int,
    budget_usd: float,
    next_max_cost: float,
    include_simulated: bool,
) -> None:
    current_spent = spent(conn, campaign_id, include_simulated)
    if current_spent + next_max_cost > budget_usd:
        raise BudgetExceeded(
            f"Budget exceeded for campaign {campaign_id}: spent ${current_spent:.4f} + next ${next_max_cost:.4f} > budget ${budget_usd:.4f}"
        )


def record_llm(
    conn: sqlite3.Connection,
    usage_in: int,
    usage_out: int,
    settings,
    now: str,
    campaign_id: int | None,
    run_id: int | None,
    simulated: bool,
    detail: str,
) -> float:
    unit_in = settings.llm_input_usd_per_mtok / 1_000_000.0
    unit_out = settings.llm_output_usd_per_mtok / 1_000_000.0
    c1 = record(
        conn,
        "llm_input",
        usage_in,
        now,
        campaign_id=campaign_id,
        run_id=run_id,
        simulated=simulated,
        unit_cost=unit_in,
        detail=detail,
    )
    c2 = record(
        conn,
        "llm_output",
        usage_out,
        now,
        campaign_id=campaign_id,
        run_id=run_id,
        simulated=simulated,
        unit_cost=unit_out,
        detail=detail,
    )
    return c1 + c2


def estimate_campaign_max_cost(campaign_row, settings) -> dict:
    handles_str = (
        campaign_row["handles"] if hasattr(campaign_row, "__getitem__") else getattr(campaign_row, "handles", "")
    ) or ""
    handles = [h.strip() for h in handles_str.replace("\n", ",").split(",") if h.strip()]
    num_handles = len(handles)

    search_kw = (
        campaign_row["search_keywords"]
        if hasattr(campaign_row, "__getitem__")
        else getattr(campaign_row, "search_keywords", "")
    ) or ""
    search_max = (
        (campaign_row["search_max_posts"] if hasattr(campaign_row, "__getitem__") else getattr(campaign_row, "search_max_posts", 100))
        if search_kw.strip()
        else 0
    )

    seed_str = (
        campaign_row["seed_handles"] if hasattr(campaign_row, "__getitem__") else getattr(campaign_row, "seed_handles", "")
    ) or ""
    seeds = [s.strip() for s in seed_str.replace("\n", ",").split(",") if s.strip()]
    num_seeds = len(seeds)

    seed_following_max = (
        campaign_row["seed_following_max"]
        if hasattr(campaign_row, "__getitem__")
        else getattr(campaign_row, "seed_following_max", 100)
    )

    include_followers = bool(
        campaign_row["include_sender_followers"]
        if hasattr(campaign_row, "__getitem__")
        else getattr(campaign_row, "include_sender_followers", 1)
    )
    followers_max = (
        (campaign_row["followers_max"] if hasattr(campaign_row, "__getitem__") else getattr(campaign_row, "followers_max", 200))
        if include_followers
        else 0
    )

    max_deep = (
        campaign_row["max_deep_profiles"]
        if hasattr(campaign_row, "__getitem__")
        else getattr(campaign_row, "max_deep_profiles", 40)
    )

    handles_cost = num_handles * PRICES_USD["users_lookup"]
    search_cost = search_max * (PRICES_USD["search_posts"] + PRICES_USD["search_authors"])
    seeds_cost = num_seeds * (PRICES_USD["users_lookup"] + seed_following_max * PRICES_USD["following"])
    followers_cost = followers_max * PRICES_USD["followers_owned"]
    posts_cost = max_deep * 10 * PRICES_USD["user_posts"]
    x_usd = handles_cost + search_cost + seeds_cost + followers_cost + posts_cost

    candidates = num_handles + search_max + (num_seeds * seed_following_max) + followers_max
    tier1_calls = math.ceil(candidates / 20.0) if candidates > 0 else 0
    tier1_in = tier1_calls * 3000 * (settings.llm_input_usd_per_mtok / 1_000_000.0)
    tier1_out = tier1_calls * 1500 * (settings.llm_output_usd_per_mtok / 1_000_000.0)

    tier2_calls = max_deep
    tier2_in = tier2_calls * 2500 * (settings.llm_input_usd_per_mtok / 1_000_000.0)
    tier2_out = tier2_calls * 600 * (settings.llm_output_usd_per_mtok / 1_000_000.0)

    llm_usd = tier1_in + tier1_out + tier2_in + tier2_out
    total_usd = x_usd + llm_usd

    return {
        "x_usd": round(x_usd, 4),
        "llm_usd": round(llm_usd, 4),
        "total_usd": round(total_usd, 4),
    }
