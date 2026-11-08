from bdh_sdk import PluginSpec, Tile

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
)
