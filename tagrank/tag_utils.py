"""Shared tag utilities: namespace detection and sibling-aware tag resolution."""


def has_namespace(tag: str) -> bool:
    """True if tag has a namespace prefix (colon-delimited, non-empty namespace part).

    Examples: "performer:alice" → True, "rating:safe" → True, "outdoors" → False, ":invalid" → False.
    """
    if ":" not in tag:
        return False
    namespace, _ = tag.split(":", 1)
    return bool(namespace)


def resolve_tags(service_data: dict) -> set[str]:
    """Extract final tags from a service's tag dict, respecting Hydrus's sibling collapse
    and applying the user's carve-out: unnamespaced tags are collapsed via display_tags
    (already processed by Hydrus), but namespaced tags survive even if they're the non-ideal
    side of a sibling pair (by including them from storage_tags if they appear there).

    Args:
        service_data: One entry from file_metadata["tags"][service_key], containing
                      "display_tags" and "storage_tags" dicts keyed by status ("0", "1").

    Returns:
        Set of final tag strings to use for this file/service, combining:
        - All display_tags (sibling-collapsed by Hydrus)
        - Plus any namespaced storage_tags not already in display (preserves namespaced pairs)
    """
    final_tags: set[str] = set()

    # Start with display_tags, which Hydrus has already sibling-collapsed
    display_tags = (service_data.get("display_tags") or {})
    for status in ("0", "1"):
        if status in display_tags:
            final_tags.update(display_tags[status])

    # Add any namespaced tags from storage that aren't already in final set
    # (includes namespaced tags that were collapsed away but should survive)
    storage_tags = (service_data.get("storage_tags") or {})
    for status in ("0", "1"):
        if status in storage_tags:
            for tag in storage_tags[status]:
                if has_namespace(tag) and tag not in final_tags:
                    final_tags.add(tag)

    return final_tags
