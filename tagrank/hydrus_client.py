"""Hydrus API access: client creation, file metadata fetch, and tag/sort operations."""

import itertools
import math
from typing import Tuple

import hydrus_api  # type: ignore

from config import key
from tagrank.cli_errors import (
    print_access_key_info_then_exit,
    print_connection_error_help_then_exit,
    print_could_not_fetch_file_information_then_exit,
    print_permissions_error_then_exit,
    print_verification_server_error_help_then_exit,
)
from tagrank.rating import FileMetaData, RatingSystem

try:
    from itertools import batched
except ImportError:
    def batched(iterable, n):
        if n < 1:
            raise ValueError('n must be at least one')
        it = iter(iterable)
        while batch := list(itertools.islice(it, n)):
            yield batch


def create_client_or_exit() -> hydrus_api.Client:
    access_key = key("API_KEY").strip()
    if not access_key or access_key == "FILL_ME_IN":
        print("ERROR: No API_KEY found in the config/KEYS file.")
        print_access_key_info_then_exit()
    url = key("API_URL").strip() or None
    client = hydrus_api.Client(access_key, api_url=url) if url else hydrus_api.Client(access_key)
    access_key_response = None
    try:
        access_key_response = client.verify_access_key()
    except hydrus_api.ServerError as e:
        print_verification_server_error_help_then_exit(e)
    except hydrus_api.ConnectionError as e:
        print_connection_error_help_then_exit(e)
    except hydrus_api.InsufficientAccess as e:
        print_permissions_error_then_exit(e)
    if access_key_response is None:
        print_verification_server_error_help_then_exit()
    if 3 not in access_key_response["basic_permissions"]:
        print_permissions_error_then_exit(None)
    return client


def sort_files_by_mmr(
    file_infos: list[Tuple[int, FileMetaData]], rating_system: RatingSystem
) -> list[Tuple[int, FileMetaData]]:
    return sorted(
        file_infos,
        key=lambda file_info: rating_system.file_score(file_info[1]),
        reverse=True,
    )


GET_FILE_INFO_FROM_CLIENT_CHUNK_SIZE = 1000


def get_file_infos_from_client(client: hydrus_api.Client, file_ids: list[int]) -> list[Tuple[int, FileMetaData]]:
    file_ids_to_tags: list[Tuple[int, FileMetaData]] = []

    def get_and_process_one_chunk(chunk_of_ids: list[int]):
        file_infos_response = client.get_file_metadata(file_ids=chunk_of_ids)
        if file_infos_response is None or file_infos_response["metadata"] is None:
            print_could_not_fetch_file_information_then_exit()
        file_ids_to_tags.extend((info["file_id"], info) for info in file_infos_response["metadata"])

    if len(file_ids) < GET_FILE_INFO_FROM_CLIENT_CHUNK_SIZE:
        get_and_process_one_chunk(file_ids)
        return file_ids_to_tags

    chunks = math.ceil(len(file_ids) / GET_FILE_INFO_FROM_CLIENT_CHUNK_SIZE)
    print(f"Getting file info from the client in {chunks} chunks.")
    print("Chunks done: 0", end="")
    for (index, id_batch) in enumerate(batched(file_ids, GET_FILE_INFO_FROM_CLIENT_CHUNK_SIZE), start=1):
        get_and_process_one_chunk(id_batch)
        print(f"\rChunks done: {index}", end="", flush=True)
    print("\rChunks done: ALL")
    return file_ids_to_tags


def delete_existing_sort_tags_if_needed(client: hydrus_api.Client) -> None:
    response = client.search_files(tags=["TagRankSort:*"])
    if response is None or response["file_ids"] is None:
        print("I was not able to search for files or something went wrong when trying to.")
        print("Please check your permissions with the following help text.")
        print("If this does not help please report this error.")
        print_permissions_error_then_exit(None)
    if len(response["file_ids"]) == 0:
        return
    print("You still have files with the TagRankSort tags from an earlier sort attempt!")
    still_has_tags_response = get_file_infos_from_client(client, response["file_ids"])
    for (file_id, metadata) in still_has_tags_response:
        for (tag_repo_identifier, tag_repo_data) in metadata["tags"].items():
            if "0" not in tag_repo_data["display_tags"]:
                continue
            previous_sort_tags = [tag for tag in tag_repo_data["display_tags"]["0"] if tag.startswith("TagRankSort:")]
            if len(previous_sort_tags) > 0:
                client.add_tags(file_ids=[file_id], service_keys_to_actions_to_tags={
                    tag_repo_identifier: {hydrus_api.TagAction.DELETE: previous_sort_tags}})
    print("Existing sort tags deleted.")
