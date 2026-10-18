"""Reusable Combee aggregation layers; full method recipes compose these APIs."""


def register(registry):
    from proteus.methods.gepa.state import register_reflection_data
    from proteus.operators.combee.ace import AceAggregation, AceRecord
    from proteus.operators.combee.gepa import GepaAggregation
    from proteus.operators.combee.grouping import GroupPlan
    from proteus.operators.combee.managed import Wave

    register_reflection_data(registry)
    for name, schema in (
        ("plan", GroupPlan),
        ("wave", Wave),
        ("ace_record", AceRecord),
        ("ace_result", AceAggregation),
        ("gepa_result", GepaAggregation),
    ):
        registry.register_data(f"proteus.combee.{name}@1", schema)
