import json
import re

from app.llm.base import LLMResult

TIER1_PASS_KEYWORDS = [
    "robot",
    "robotics",
    "llm",
    "language model",
    "agent",
    "inference",
    "gpu",
    "embodied",
    "machine learning",
    "ml engineer",
    "ai startup",
    "founder",
    "phd",
]

TIER1_FAIL_KEYWORDS = [
    "news",
    "official",
    "we are",
    "giveaway",
    "token",
    "airdrop",
]

SEA_LOCATIONS = [
    "singapore",
    "vietnam",
    "hanoi",
    "ho chi minh",
    "jakarta",
    "indonesia",
    "bangkok",
    "thailand",
    "kuala lumpur",
    "malaysia",
    "manila",
    "philippines",
    "southeast asia",
]


def _find_sentence_with_keyword(text: str, keywords: list[str]) -> str:
    if not text:
        return ""
    # Split text into sentences
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    for s in sentences:
        s_clean = s.strip()
        s_lower = s_clean.lower()
        if any(kw in s_lower for kw in keywords):
            return s_clean.rstrip(".")
    return text.strip()


class FakeLLM:
    def call_tool(
        self,
        system: str,
        user: str,
        tool_name: str,
        tool_schema: dict,
        max_tokens: int = 2000,
    ) -> LLMResult:
        input_tokens = len(system + user) // 4
        output_tokens = 200

        if "Profiles (JSON):" in user:
            # Tier 1 screening
            idx = user.find("Profiles (JSON):\n")
            json_str = user[idx + len("Profiles (JSON):\n") :]
            profiles = json.loads(json_str)

            results = []
            for prof in profiles:
                bio = prof.get("bio", "")
                post = prof.get("post", "")
                combined = f"{bio} {post}".lower()

                has_pass = any(kw in combined for kw in TIER1_PASS_KEYWORDS)
                has_fail = any(kw in combined for kw in TIER1_FAIL_KEYWORDS)
                passed = has_pass and not has_fail

                if passed:
                    quote = _find_sentence_with_keyword(bio or post, TIER1_PASS_KEYWORDS)
                    # Quote must be between 5 and 25 words
                    words = quote.split()
                    if len(words) > 25:
                        quote = " ".join(words[:20])
                    results.append(
                        {
                            "x_user_id": str(prof["x_user_id"]),
                            "pass": True,
                            "reason": "Profile matches AI or robotics builder criteria",
                            "quote": quote,
                        }
                    )
                else:
                    results.append(
                        {
                            "x_user_id": str(prof["x_user_id"]),
                            "pass": False,
                            "reason": "Profile does not show AI or robotics work",
                            "quote": "",
                        }
                    )
            return LLMResult(
                data={"results": results},
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        elif "User (JSON):" in user:
            # Tier 2 scoring
            idx = user.find("User (JSON):\n")
            json_str = user[idx + len("User (JSON):\n") :]
            u_data = json.loads(json_str)

            bio = u_data.get("bio", "")
            loc = u_data.get("location", "")
            posts = u_data.get("posts", [])
            combined_posts = " ".join(p.get("text", "") for p in posts)
            all_text = f"{bio} {loc} {combined_posts}".lower()

            criteria = []

            # 1. Role
            role_kws = ["builder", "engineer", "developer", "founder", "researcher", "phd"]
            has_hobby = "hobby" in all_text or "tinkering" in all_text
            role_met = any(kw in all_text for kw in role_kws) and not has_hobby
            role_ev = []
            if role_met:
                quote = _find_sentence_with_keyword(bio, role_kws)
                if quote:
                    role_ev.append({"source": "bio", "quote": quote})
                elif posts:
                    p = posts[0]
                    role_ev.append({"source": "post", "post_id": str(p["post_id"]), "quote": _find_sentence_with_keyword(p.get("text", ""), role_kws)})
            criteria.append({"name": "role", "met": role_met, "evidence": role_ev[:2]})

            # 2. Domain
            domain_kws = ["robot", "robotics", "llm", "language model", "agent", "inference", "gpu", "embodied", "machine learning"]
            domain_met = any(kw in all_text for kw in domain_kws) and not has_hobby
            domain_ev = []
            if domain_met:
                quote = _find_sentence_with_keyword(bio, domain_kws)
                if quote:
                    domain_ev.append({"source": "bio", "quote": quote})
                if posts:
                    for p in posts:
                        if any(kw in p.get("text", "").lower() for kw in domain_kws):
                            domain_ev.append({"source": "post", "post_id": str(p["post_id"]), "quote": _find_sentence_with_keyword(p.get("text", ""), domain_kws)})
                            break
            criteria.append({"name": "domain", "met": domain_met, "evidence": domain_ev[:2]})

            # 3. Individual
            fail_kws = ["news", "official", "we are", "giveaway", "token", "airdrop"]
            indiv_met = not any(kw in all_text for kw in fail_kws)
            indiv_ev = []
            if indiv_met and bio:
                indiv_ev.append({"source": "bio", "quote": bio.split(".")[0].strip()})
            criteria.append({"name": "individual", "met": indiv_met, "evidence": indiv_ev[:2]})

            # 4. Campaign Fit
            fit_met = any(kw in all_text for kw in SEA_LOCATIONS)
            fit_ev = []
            if fit_met:
                quote = _find_sentence_with_keyword(bio, SEA_LOCATIONS)
                if quote:
                    fit_ev.append({"source": "bio", "quote": quote})
                elif posts:
                    for p in posts:
                        if any(kw in p.get("text", "").lower() for kw in SEA_LOCATIONS):
                            fit_ev.append({"source": "post", "post_id": str(p["post_id"]), "quote": _find_sentence_with_keyword(p.get("text", ""), SEA_LOCATIONS)})
                            break
            criteria.append({"name": "campaign_fit", "met": fit_met, "evidence": fit_ev[:2]})

            # Decide role & segment
            if "founder" in all_text:
                role = "founder"
            elif "researcher" in all_text or "phd" in all_text:
                role = "researcher"
            elif any(k in all_text for k in ["engineer", "builder", "developer"]):
                role = "builder"
            else:
                role = "other"

            if "robot" in all_text:
                segment = "robotics"
            elif "embodied" in all_text:
                segment = "embodied_ai"
            elif "inference" in all_text or "gpu" in all_text:
                segment = "infrastructure"
            elif "agent" in all_text or "tool" in all_text:
                segment = "developer_tools" if "tool" in all_text else "applications"
            elif "llm" in all_text or "language model" in all_text:
                segment = "models"
            else:
                segment = "other"

            why_fit = ["Builder working actively on AI and robotics systems.", "Demonstrated technical evidence in profile."]
            caution = None if indiv_met else "Account appears non-individual or promotional."

            return LLMResult(
                data={
                    "criteria": criteria,
                    "role": role,
                    "segment": segment,
                    "why_fit": why_fit,
                    "caution": caution,
                },
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        raise ValueError(f"FakeLLM cannot handle unexpected prompt format: {user[:100]}")
