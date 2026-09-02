"""Entrypoint/orchestration: main(mode) wiring rating, hydrus_client, and ui together."""

import math
import sys
from importlib.metadata import version
from pathlib import Path

from PySide6 import QtWidgets
from trueskill import Rating  # type: ignore

from config import ensure_config_files
from tagrank.cli_errors import (
    print_add_tags_permissions_missing_info_then_exit,
    print_could_not_fetch_file_information_then_exit,
    print_no_relevant_files_then_exit,
    print_no_relevant_files_to_sort_then_exit,
    print_permissions_error_then_exit,
)
from tagrank.errors import FileInformationError
from tagrank.graphs import top_tags_from_rating_system
from tagrank.hydrus_client import (
    create_client_or_exit,
    delete_existing_sort_tags_if_needed,
    get_file_infos_from_client,
    sort_files_by_mmr,
)
from tagrank.rating import RatingSystem, trueskill_number_from_rating
from tagrank.settings import Settings, load_settings
from tagrank.ui.summary_dashboard import SummaryDashboard
from tagrank.ui.window import Window

import hydrus_api  # type: ignore


def _check_hydrus_api_version() -> None:
    h_api_version = version('hydrus_api')
    if h_api_version is None:
        return
    if len(h_api_version.split(".")) < 3:
        return
    try:
        major, minor, patch = h_api_version.split(".")
        if int(major) < 5:
            print("Your hydrus_api version is not up to date!")
            print(f"Tagrank is seeing version {h_api_version}, but requires at least version 5.2.0.")
            print("You can update your hydrus_api version with the command `pip install --upgrade hydrus_api`.")
            print("If you have done so, tagrank is up to date, and this error still comes up please make a report on github or on discord.")
            print("Be sure to include the output of `pip freeze` and the error message you are now reading.")
            sys.exit(1)
    except ValueError:
        pass


def _warn_if_rating_keys_missing(settings: Settings) -> None:
    if not settings.hydrus.mmr_service_key or settings.hydrus.mmr_service_key == "FILL_ME_IN":
        print("WARNING: TAGRANK_MMR_SERVICE_KEY is not configured in config/KEYS.")
        print("  TagRank will continue without writing file MMR ratings to Hydrus until that key is set.")

    if not settings.hydrus.mmr_confidence_service_key or settings.hydrus.mmr_confidence_service_key == "FILL_ME_IN":
        print("WARNING: TAGRANK_MMR_CONFIDENCE_SERVICE_KEY is not configured in config/KEYS.")
        print("  TagRank will continue without writing file confidence ratings to Hydrus until that key is set.")


def run_for_rank_tags(client, settings: Settings, preset_tag: str | None = None, use_similarity: bool = True) -> None:
    files_path_path = Path("./FILES_PATH")
    if files_path_path.exists():
        print("WARNING: The `./FILES_PATH` file is no longer needed. You can remove it.")
        print(f"         The exact path is: {files_path_path.resolve()}")

    from tagrank.pool import build_pool, prompt_for_search
    from tagrank.presets import get_preset

    app = QtWidgets.QApplication(sys.argv)
    if preset_tag is not None:
        # Caller (e.g. Undertow's TagRank tab) already picked the tag from its own copy of the
        # Top/Random/Bottom list - skip the interactive numbered-menu prompt entirely.
        print(f"Using tag search: {preset_tag}")
        query = [preset_tag]
    else:
        query = prompt_for_search(client)  # numbered most-liked tags, 0 = custom search
    preset_id: str | None = None
    window: Window | None = None

    while True:
        effective_query = query
        pool_size_override = None
        if preset_id is not None:
            preset = get_preset(preset_id)
            if preset is not None:
                if preset.query:
                    effective_query = preset.query
                if preset.pool_size:
                    pool_size_override = preset.pool_size
                if preset.pool_strategy:
                    from tagrank.settings import get_settings_store
                    get_settings_store().update({"pool.pool_strategy": preset.pool_strategy})

        pool_kwargs = {"client": client, "query": effective_query, "use_similarity": use_similarity}
        if pool_size_override:
            pool_kwargs["pool_size"] = pool_size_override
        hashes = build_pool(**pool_kwargs)
        if not hashes:
            print_no_relevant_files_then_exit(effective_query)

        metadata_response = client.get_file_metadata(hashes=hashes)
        if metadata_response is None or metadata_response.get("metadata") is None:
            print_could_not_fetch_file_information_then_exit()

        ids = [int(meta["file_id"]) for meta in metadata_response["metadata"] if "file_id" in meta]

        if len(ids) < 2:
            print_no_relevant_files_then_exit(effective_query)

        rating_system = RatingSystem(client, ids, load_settings())

        dashboard = SummaryDashboard(rating_system, settings.ui.amount_of_tags_in_charts)
        window = Window(rating_system, client, on_change=dashboard.refresh, dashboard=dashboard)

        window.show()
        screen_geometry = window.screen().availableGeometry() if window.screen() else None
        if screen_geometry is not None:
            window.setGeometry(
                screen_geometry.x(), screen_geometry.y(),
                screen_geometry.width() * 3 // 5, screen_geometry.height()
            )
            dashboard.setGeometry(
                screen_geometry.x() + screen_geometry.width() * 3 // 5, screen_geometry.y(),
                screen_geometry.width() * 2 // 5, screen_geometry.height()
            )
        dashboard.show()

        first_section_result = app.exec()
        if first_section_result != 0:
            print("Comparison app closed in error. Not moving on to comparisons.")
            sys.exit(first_section_result)

        if window.restart_preset_id is not None:
            preset_id = window.restart_preset_id
            window.destroy()
            continue
        break

    window.destroy()

    many_tags: list[tuple[str, Rating]] = top_tags_from_rating_system(rating_system, settings.ui.amount_of_tags_in_charts)

    largest_mu_width = len(str(math.floor(trueskill_number_from_rating(many_tags[0][1]))))
    print("The window that shows the scores can be hard to read. So here the data in text for 10 tags:")
    for (tag, rating) in many_tags:
        print(f"{trueskill_number_from_rating(rating):.1f}".rjust(largest_mu_width + 3) + f": {tag}")


def run_for_create_image_ranking(client: hydrus_api.Client, settings: Settings) -> None:
    if hydrus_api.Permission.ADD_TAGS not in client.verify_access_key()["basic_permissions"]:
        print_add_tags_permissions_missing_info_then_exit()
    delete_existing_sort_tags_if_needed(client, settings)
    rating_system = RatingSystem(client, [], settings)
    tags = list(rating_system.current_ratings.keys())
    search_kwargs: dict = {}
    if settings.hydrus.tag_service_key:
        search_kwargs["tag_service_key"] = settings.hydrus.tag_service_key
    if settings.pool.file_service_key:
        search_kwargs["file_service_keys"] = [settings.pool.file_service_key]
    # noinspection PyTypeChecker
    response = client.search_files(tags=[tags], **search_kwargs)
    if response is None or response["file_ids"] is None or len(response["file_ids"]) == 0:
        print_no_relevant_files_to_sort_then_exit()
    file_ids = [int(file_id) for file_id in response["file_ids"]]
    print(f"Found {len(file_ids)} files that have at least one ranked tag.")
    try:
        file_infos = get_file_infos_from_client(client, file_ids)
    except FileInformationError:
        print_could_not_fetch_file_information_then_exit()
    print("Got metadata and direct MMR ratings for each file from the client.")
    print("Now sorting the list by tagrankMMR...")
    sorted_file_infos = sort_files_by_mmr(file_infos, rating_system)
    print("Sorted the list. Now setting the sort-order tags in hydrus.")
    found_service_id = settings.hydrus.badge_tag_service_key or settings.hydrus.tag_service_key or None
    if found_service_id is None:
        services_response = client.get_services()
        services_map = services_response["services"]
        for service_id, service_data in services_map.items():
            if service_data["type"] == hydrus_api.ServiceType.TAG_DOMAIN:
                if found_service_id is None:
                    found_service_id = service_id
                if service_data["name"] == "my tags":
                    found_service_id = service_id
    for (index, (file_id, _)) in enumerate(sorted_file_infos):
        client.add_tags(file_ids=[file_id], service_keys_to_tags={found_service_id: [f"TagRankSort:{index}"]})
    print("Have sent all the tags to the client.")
    print("DONE! If you need info on how to use this to sort your files, read below:")
    print("  You can use this sort order by clicking the 'sort by(...)' button on the top left of a file search column. ")
    print("  Here, select Namespaces -> Custom. Then fill in 'TagRankSort'. Press ok, select 'display tags'.")
    print("  If you want to make this easier, go to: file -> options -> sort/collect.")
    print("  In the 'namespace file sorting' section press 'add' at the bottom.")
    print("  Fill in 'TagRankSort', press ok, then select 'display tags'.")
    print("  Press apply to save these settings.")
    print("  Now, if you want to set this as the default sort: go to: file -> options -> sort/collect.")
    print("  Click the first button to the right of the text 'Default File Sort'")
    print("  Here, select Namespaces, and click the 'sort by tags: TagRankSort' option that you just created.")
    print()
    input("Press Enter to exit...")


def run_for_serve(port: int) -> None:
    """Run the headless HTTP API (tagrank/server.py) instead of the GUI. See docs/api.md."""
    import uvicorn
    from tagrank.server import app as fastapi_app

    print(f"TagRank API listening on http://127.0.0.1:{port} (docs at /docs).")
    uvicorn.run(fastapi_app, host="127.0.0.1", port=port)


MODE_CREATE_IMAGE_RANKING = "create_image_ranking"
MODE_RANK_TAGS = "rank_tags"
MODE_SERVE = "serve"


def main(mode: str, *, port: int = 8420, preset_tag: str | None = None, use_similarity: bool = True) -> None:
    ensure_config_files()
    _check_hydrus_api_version()
    settings = load_settings()
    _warn_if_rating_keys_missing(settings)
    if mode == MODE_SERVE:
        run_for_serve(port)
        return
    client = create_client_or_exit(settings)
    if mode == MODE_RANK_TAGS:
        run_for_rank_tags(client, settings, preset_tag=preset_tag, use_similarity=use_similarity)
    elif mode == MODE_CREATE_IMAGE_RANKING:
        run_for_create_image_ranking(client, settings)
    else:
        print("ERROR: Unknown run mode!")
