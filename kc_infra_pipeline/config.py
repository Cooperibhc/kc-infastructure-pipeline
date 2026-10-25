"""Per-city configuration."""

from dataclasses import dataclass, field

from . import __version__

USER_AGENT = (
    f"kc-infra-pipeline/{__version__} "
    "(+https://github.com/Cooperibhc/kc-infastructure-pipeline; civic research bot)"
)
REQUEST_DELAY_SECONDS = 1.0
DEFAULT_LOOKBACK_DAYS = 14


@dataclass(frozen=True)
class CityConfig:
    slug: str
    name: str
    legistar_client: str
    bodies: tuple = field(default_factory=tuple)

    @property
    def api_base(self) -> str:
        return f"https://webapi.legistar.com/v1/{self.legistar_client}"

    @property
    def site_base(self) -> str:
        return f"https://{self.legistar_client}.legistar.com"


CITIES = {
    "lees-summit": CityConfig(
        slug="lees-summit",
        name="Lee's Summit, MO",
        legistar_client="lsmo",
        bodies=("City Council", "Public Works Committee", "Planning Commission"),
    ),
}
