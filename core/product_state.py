import asyncio
import json
import re
from pathlib import Path

from core.state_manager import write_atomic


VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,159}")


class ProductError(ValueError):
    pass


class ProductForbidden(ProductError):
    pass


def version_name(value):
    if not isinstance(value, str) or len(value) > 40 or not VERSION.fullmatch(value):
        raise ProductError("Use a release version in vN.N.N form")
    return value


def identity(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ProductError("Invalid product identifier")
    return value


def repository_key(value):
    if type(value) is not int or value <= 0:
        raise ProductError("Invalid GitHub repository identity")
    return str(value)


class ProductStore:
    """Owned by the same locked Python process that owns the legacy DAGs."""

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "products.json"
        self.lock = asyncio.Lock()

    def read(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": 1, "products": {}}
        if not isinstance(state, dict) or state.get("schema") != 1 or not isinstance(state.get("products"), dict):
            raise ProductError("Unsupported managed-product state")
        return state

    def save(self, state):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_atomic(self.path, json.dumps(state, ensure_ascii=False, allow_nan=False) + "\n")

    def put(self, state, product):
        state["products"][repository_key(product["repository_id"])] = product
        self.save(state)

    def product(self, state, repository_id):
        product = state["products"].get(repository_key(repository_id))
        if product is None:
            raise ProductError("Unknown managed product")
        return product

    def workspace(self, repository_id, version):
        return self.root / repository_key(repository_id) / "versions" / version_name(version)


def bind_product(repository, *, title, actor, pm, usage, rollback=False, budget):
    repository_id = repository.get("id")
    repository_key(repository_id)
    roles = {"chief": identity(actor), "pm": identity(pm), "usage": identity(usage)}
    if len(set(roles.values())) != len(roles):
        raise ProductError("Choose distinct Chief, PM and Usage Bot identities")
    if repository.get("visibility") != "private":
        raise ProductError("Managed workflow products require a private GitHub repository")
    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        raise ProductError("A product title is required")
    if type(budget) not in (int, float) or not 0 < budget <= 10000:
        raise ProductError("Set a finite positive project budget")
    return {
        "repository_id": repository_id, "repository": repository, "title": title.strip(),
        "roles": roles, "grant": {"target": "ds-bot-local", "budget": budget, "rollback": rollback is True},
        "versions": {}, "issues": {}, "runs": {}, "pending_issues": {}, "issue_notices": {},
    }


def authorize(product, actor, *roles):
    if not isinstance(actor, str) or not any(product["roles"].get(role) == actor for role in roles):
        raise ProductForbidden("This conversation has no permission for the product operation")


def register_version(product, version, base, issue_numbers):
    version_name(version)
    versions = product["versions"]
    if version in versions:
        existing = versions[version]
        if (base, issue_numbers) != (existing["base"], existing["issue_numbers"]):
            raise ProductError("Version already exists with a different plan")
        return existing
    if base is not None and (base not in versions or not versions[base].get("released")):
        raise ProductError("Choose an explicitly released base version")
    if versions and base is None:
        raise ProductError("A subsequent version requires a released base")
    if any(not v.get("released") and not v.get("interrupted") for v in versions.values()):
        raise ProductError("A version already has a development plan")
    if any(product["issues"].get(str(i), {}).get("triage") != "accepted" for i in issue_numbers):
        raise ProductError("Only accepted issues can enter a version plan")
    record = {"version": version, "base": base, "issue_numbers": issue_numbers, "project_id": None}
    versions[version] = record
    return record
