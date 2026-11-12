from bdh_sdk import PluginSpec, Setting, Tile
from bdh_sdk.plugins import BOOL, INT

_KIND_HELP = "Show these in the Last 24 hours panel and on the timeline (signed-in admins only)."

spec = PluginSpec(
    id="cameras",
    app="cameras",
    urls="cameras.urls",
    status="cameras.homepage.status",
    tiles=(
        Tile(
            title="Cameras",
            url_name="cameras",
            icon="cameras/hub/icon.svg",
            section="more",
            subtitle="Outdoor cameras",
            order=20,
        ),
    ),
    settings=(
        Setting("show_doorbell", "Doorbell rings", type=BOOL, default=True, help=_KIND_HELP),
        Setting("show_person", "People", type=BOOL, default=True, help=_KIND_HELP),
        Setting("show_vehicle", "Vehicles", type=BOOL, default=True, help=_KIND_HELP),
        Setting("show_package", "Packages", type=BOOL, default=True, help=_KIND_HELP),
        Setting("show_animal", "Animals", type=BOOL, default=False, help=_KIND_HELP),
        Setting("show_motion", "Plain motion", type=BOOL, default=False,
                help="Motion with no person, vehicle, package or animal detected."),
        Setting("doorbell_popup", "Doorbell pop-up", type=BOOL, default=True,
                help="Show the doorbell camera live on the hub screen when someone rings."),
        Setting("event_popup", "Event pop-ups", type=BOOL, default=True,
                help="Show a camera live when one of the site's event_popups fires, "
                     "e.g. the front door opening."),
        Setting("popup_seconds", "Pop-up duration", type=INT, default=60,
                minimum=10, maximum=600, unit="s"),
    ),
)