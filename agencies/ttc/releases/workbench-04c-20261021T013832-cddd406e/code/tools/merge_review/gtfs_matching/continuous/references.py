"""Agency namespace dispatch to physically constrained, validated references."""

from ..canonical import build_references
from ..projection import project_sequence


def build_agency_references(bundles, config=None):
    config = dict(config or {}, continuous_matching_enabled=True)
    output = {}
    for bundle in bundles:
        key = (str(bundle["feed_version"]), str(bundle["route_id"]))
        if key in output:
            raise ValueError("Duplicate active feed/route namespace")
        output[key] = build_references(bundle, config)
    return {
        "namespaces": output,
        "route_independent": True,
        "cross_reference_progress_comparable": False,
    }


def project_agency_hypotheses(avl, apc, agency_references, config=None):
    """Retain independent route hypotheses; never join incomparable progress axes.

    Inside each namespace all directions/families undergo physical sequence search,
    including the alternatives to the AVL direction prior, before distance gating.
    """
    return {
        key: project_sequence(avl, bundle["references"], config, apc=apc)
        for key, bundle in agency_references["namespaces"].items()
    }
