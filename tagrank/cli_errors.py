"""User-facing help/error messages for every documented failure mode, each exiting the process."""

import sys
from pathlib import Path
from typing import NoReturn

import hydrus_api  # type: ignore

from config import DATA_DIR


def print_could_not_read_comparisons_file_help() -> None:
    comparisons_path = DATA_DIR / "comparisons.json"
    print(f"ERROR: Was not able to read your comparisons.json file!")
    print(f"  The reason for this will be printed above, or below this information.")
    print(f"  If you do not know what the reason means you should do the following:")
    print(f"  1. Rename the file {comparisons_path.resolve()} to something else.")
    print(f"  2. Show the error and the file to me in the hydrus discord if you want to recover the comparisons.")
    print(f"  3. Re-open TagRank, it will start your comparisons list from new.", flush=True)


def print_access_key_info_then_exit() -> NoReturn:
    print("  You need to create a client api service via services->review services->local->client api->add->manually")
    print("  It needs to have the permission search and fetch files.")
    print("  You can blacklist any tags you want, but they won't get ranked if this program cannot see them.")
    print("  When you have done this, open the config/KEYS file (created on first run)")
    print("  and set API_KEY to your access key. The file is git-ignored.")
    print("  Then exit these windows by pressing apply.")
    print()
    print("  Now you need to turn on the client API.")
    print_enable_client_api_help()
    print()
    print("  If you have a non-standard URL or PORT set API_URL in config/KEYS too,")
    print("  e.g. 'http://127.0.0.1:45869/'.")
    sys.exit(0)


def print_verification_server_error_help_then_exit(e: None | hydrus_api.ServerError = None) -> NoReturn:
    print("ERROR: Something went wrong trying to verify your access key.")
    print("  Try re-creating your client api and saving the new access key. If need info on how. Re-set API_KEY in config/KEYS and restart TagRank.")
    if e is not None:
        print("  If that does not solve your issue, then look at the error that hydrus gave me below.")
        print("  Read it all, but the last line is probably where you'll find what is wrong.")
        print("This is what the server told me:")
        print(e)
    sys.exit(0)


def print_connection_error_help_then_exit(e: hydrus_api.ConnectionError) -> NoReturn:
    print("ERROR: Was not able to connect to hydrus.")
    print("  Are you sure your hydrus client is on?")
    print("  If it is, ensure that the API itself is on.")
    print_enable_client_api_help()
    print("  This is the error that caused the connection problem:")
    print(e)
    sys.exit(0)


def print_enable_client_api_help():
    print("  Go to Services -> Manage Services -> (double click) client api.")
    print("  Then ensure that the 'run the client api?' tick-box is on.")
    print("  Exit these windows by pressing apply.")


def print_permissions_error_then_exit(e: (hydrus_api.InsufficientAccess | None) = None) -> NoReturn:
    print("ERROR: This access key is not allowed to search for and fetch files.")
    print("  Please allow this permission for the access key you set in the config/KEYS file.")
    print("  You can find this setting at: services->review services->local->client api")
    print()
    if e is not None:
        print("We know this because the client returned the following error: ")
        print(e)
    sys.exit(0)


def print_no_relevant_files_then_exit(query: list[str]) -> NoReturn:
    print(f"ERROR: Was not able to find enough files in the client to compare.")
    print(f"  Are you sure I am allowed to search for files?")
    print(f"  I am specifically searching for files that are found by searching for the following query:")
    print(f"  {', '.join(query)}")
    print(f"  If this query looks weird, check your selection.")
    sys.exit(0)


def print_empty_query_help_then_exit() -> NoReturn:
    print("ERROR: the file query is empty.")
    print("Since this may lead to very large queries, this is not allowed.")
    print("If you really want the search to return all files, add 'system: everything'.")
    sys.exit(0)


def print_could_not_fetch_file_information_then_exit() -> NoReturn:
    print("ERROR: Was not able to fetch file information.")
    print("  Are you sure that I have all the needed permissions?")
    sys.exit(0)


def print_no_relevant_files_to_sort_then_exit() -> NoReturn:
    print("ERROR: Was not able to find any files to sort.")
    print("  Are you sure you have any ranked tags?")
    sys.exit(0)


def print_add_tags_permissions_missing_info_then_exit() -> NoReturn:
    print("ERROR: TagRank is not allowed to add tags to the client!")
    print("  In order to add the ranking tags to the client TagRank needs the 'edit file tags' permission.")
    print("  You can set this up by going to the following:")
    print("  Services -> Review Services -> local -> client api")
    print("  In this window, select the TagRank client api, then press 'edit' at the bottom of the screen.")
    print("  Now, in this window, check the checkbox before 'edit file tags'.")
    print("  Exit the window by pressing 'apply', then press 'close' to close the review services window.")
    print("  After you've done that, re-run TagRank.")
    sys.exit(0)
