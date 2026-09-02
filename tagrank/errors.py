"""Catchable exceptions for failure modes that the CLI path reports via cli_errors' print_*_then_exit
functions. The API layer (tagrank/server.py) catches these and maps them to HTTP responses instead."""


class TagRankError(Exception):
    """Base class for all TagRank service-layer errors."""


class MissingApiKeyError(TagRankError):
    """config/KEYS has no API_KEY set."""


class HydrusVerificationError(TagRankError):
    """The Hydrus server returned an error while verifying the access key."""

    def __init__(self, server_error: Exception | None = None):
        self.server_error = server_error
        super().__init__(str(server_error) if server_error else "Hydrus access key verification failed")


class HydrusConnectionError(TagRankError):
    """Could not reach the Hydrus client API (client off, or API disabled)."""

    def __init__(self, cause: Exception):
        self.cause = cause
        super().__init__(str(cause))


class HydrusPermissionError(TagRankError):
    """The configured access key lacks a required permission."""

    def __init__(self, cause: Exception | None = None):
        self.cause = cause
        super().__init__(str(cause) if cause else "Insufficient Hydrus API permissions")


class NoRelevantFilesError(TagRankError):
    """The search query returned too few files to build a comparison pool."""

    def __init__(self, query: list[str]):
        self.query = query
        super().__init__(f"No relevant files found for query: {', '.join(query)}")


class EmptyQueryError(TagRankError):
    """An empty query was supplied without an explicit 'system:everything'."""


class FileInformationError(TagRankError):
    """Could not fetch file metadata from Hydrus."""


class NoFilesToSortError(TagRankError):
    """No ranked tags exist, so there is nothing to sort."""


class MissingAddTagsPermissionError(TagRankError):
    """The access key lacks the 'edit file tags' permission needed to write sort tags."""


class UnknownServiceKeyError(TagRankError):
    """A caller-supplied file_service_key/tag_service_key doesn't match any service Hydrus
    currently knows about (per GET /get_services)."""

    def __init__(self, service_key: str):
        self.service_key = service_key
        super().__init__(f"Unknown Hydrus service key: '{service_key}'")
