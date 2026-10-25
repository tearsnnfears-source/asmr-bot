"""New Tribute checkout catalog. No legacy links or client-supplied prices."""
import os

PROJECTS = frozenset({"asmrleaks", "privateleaks", "asianleaks", "extraleaks"})
PRODUCTS = {
    ("asmrleaks", "plus"): ("s1ajK", 600),
    ("privateleaks", "plus"): ("s1ajL", 550),
    ("asianleaks", "plus"): ("s1ajN", 525),
    ("extraleaks", "plus"): ("s1ajP", 300),
    ("asmrleaks", "pro"): ("s1ajR", 800),
    ("asmrleaks", "elite"): ("s1ajS", 1000),
    ("asmrleaks", "king"): ("s1ajU", 1300),
}


def checkout_enabled():
    return os.getenv("TRIBUTE_CHECKOUT_ENABLED", "false").strip().lower() == "true"


def checkout_link(project, tier):
    if not checkout_enabled():
        return ""
    product = PRODUCTS.get(("asmrleaks", tier) if tier in {"elite", "king"} else (project, tier))
    return f"https://t.me/tribute/app?startapp={product[0]}" if product else ""


def checkout_details(project, tier, target=None):
    if not checkout_enabled():
        raise ValueError("Card and SBP checkout is not available yet")
    if project not in PROJECTS:
        raise ValueError("Invalid project")
    url = checkout_link(project, tier)
    if not url:
        raise ValueError("Invalid plan")
    target = str(target or "").strip().lower() or None
    if tier == "elite" and target not in PROJECTS - {project}:
        raise ValueError("Choose the second page for ELITE")
    if tier == "king":
        if target not in {None, "all"}:
            raise ValueError("Invalid KING target")
        target = "all"
    elif tier != "elite" and target:
        raise ValueError("This plan has no second page")
    code, cents = PRODUCTS[("asmrleaks", tier) if tier in {"elite", "king"} else (project, tier)]
    return {"url": url, "product_code": code, "price_cents": cents, "currency": "EUR",
            "tier": tier, "target_project": target}
