"""Where the cloud-specific knowledge lives, so DigitalOcean is *an* answer.

`deploy.py` was always plain Kubernetes and Docker. `envs.py` and `cloud.py` were
not, and nothing said so. Four vendor facts were spread through them as if they
were facts about clusters in general:

  - the registry lives at `DOCR_REGISTRY` and images are named `<registry>/<tag>`
  - the pull credential is a secret called `docr-creds`, because DOCR is private
    and without it pods sit in ImagePullBackOff reporting "image not found"
  - listing what has been published means DigitalOcean's registry API
  - a preview may never be a NodePort: DOKS reconciles `k8s-public-access-<cluster>`
    from the NodePort services it finds and opens them to 0.0.0.0/0, so exposure
    becomes the default and any restriction is something you hold *against* the
    platform's own controller

They are gathered here and nowhere else. This is a seam, not a framework: it
carries exactly what the DigitalOcean path already needed and nothing invented
for a provider that does not exist yet. An abstraction with one implementation is
a guess about the second one; keeping it to observed facts keeps the guess small
and keeps the DO path readable as what it is.

Selection is by `CLOUD_PROVIDER`, defaulting to whichever the configuration
already implies — a `DOCR_REGISTRY` means DigitalOcean, and no registry at all
means a local kind cluster, which needs none of this.
"""

from typing import Any

from . import config


class Provider:
    """The generic cluster: plain Kubernetes, no registry, nothing private.

    This is what a local kind cluster is, and it is also the honest base for any
    provider whose registry is public or whose credentials are already on the
    nodes. Everything below it overrides only what it actually does differently.
    """

    name = "generic"

    @property
    def registry(self) -> str:
        """Prefix images must carry for the cluster's nodes to pull them, or ''."""
        return config._env("DEPLOY_REGISTRY")

    @property
    def pull_secret(self) -> str:
        """Secret name pods need in order to pull from a private registry.

        Only consulted when there IS a registry — a kind cluster is handed the
        image directly and never pulls anything.
        """
        return config._env("IMAGE_PULL_SECRET", "registry-creds")

    def image_ref(self, tag: str) -> str:
        """The name the CLUSTER pulls by. A local tag means nothing to a remote node."""
        reg = self.registry
        if not reg or tag.startswith(reg):
            return tag
        return f"{reg}/{tag}"

    @property
    def service_type(self) -> str:
        """How a non-production environment is reached from outside the cluster.

        ClusterIP plus `kubectl port-forward` costs nothing and exposes nothing,
        which is the right default everywhere and the only safe one on DOKS.
        """
        return "ClusterIP"

    async def published_tags(self, repository: str, limit: int = 10) -> list[dict[str, Any]]:
        """What has been built and pushed, newest first. Empty when we cannot look.

        Watching the registry rather than git is deliberate: an image that exists
        is a thing you can run, whereas a merged commit might still be building or
        might have failed to build at all.
        """
        return []


class DigitalOcean(Provider):
    name = "digitalocean"

    @property
    def registry(self) -> str:
        # e.g. registry.digitalocean.com/my-reg
        return config._env("DOCR_REGISTRY")

    @property
    def pull_secret(self) -> str:
        return config._env("IMAGE_PULL_SECRET", "docr-creds")

    async def published_tags(self, repository: str, limit: int = 10) -> list[dict[str, Any]]:
        import httpx
        # A read-only registry token, NOT the account token. The account token can
        # create and destroy clusters; something that only needs to list image tags
        # has no business holding it. DOCR issues scoped, expiring credentials for
        # exactly this, so least privilege costs nothing here.
        token = config._env("DOCR_READ_TOKEN") or config._env("DIGITALOCEAN_API_TOKEN")
        reg = self.registry.rstrip("/").split("/")[-1] if self.registry else ""
        if not token or not reg:
            return []
        url = (f"https://api.digitalocean.com/v2/registry/{reg}"
               f"/repositories/{repository}/tags?per_page=50")
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
                r.raise_for_status()
                tags = r.json().get("tags", [])
        except Exception:
            return []
        tags.sort(key=lambda t: t.get("updated_at", ""), reverse=True)
        return [{"tag": f"{self.registry}/{repository}:{t['tag']}", "short": t["tag"],
                 "updated_at": t.get("updated_at", "")} for t in tags[:limit]]


_IMPLEMENTATIONS = {p.name: p for p in (Provider(), DigitalOcean())}


def current() -> Provider:
    """The provider this instance is configured for.

    Resolved on every call rather than cached at import: the tests, and an
    operator changing configuration on a running instance, both need the answer to
    follow the environment rather than whatever was true at startup.
    """
    chosen = config._env("CLOUD_PROVIDER").strip().lower()
    if chosen in _IMPLEMENTATIONS:
        return _IMPLEMENTATIONS[chosen]
    # Unset: infer it. A DOCR registry is only ever DigitalOcean's, and inferring
    # keeps every existing deployment behaving exactly as it did without anyone
    # having to set a new variable.
    if config._env("DOCR_REGISTRY"):
        return _IMPLEMENTATIONS["digitalocean"]
    return _IMPLEMENTATIONS["generic"]


def describe() -> dict[str, Any]:
    p = current()
    return {"provider": p.name, "registry": p.registry, "pull_secret": p.pull_secret,
            "service_type": p.service_type}
