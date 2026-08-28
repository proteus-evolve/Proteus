from proteus.safety.schedule import (
    EveryEpisodeSchedule,
    EveryNEpisodesSchedule,
    ExplicitEpisodesSchedule,
    parse_family_schedule,
)


def selected(schedule, target=20):
    return [
        episode
        for episode in range(1, target + 1)
        if schedule.selected(episode=episode, episodes_target=target)
    ]


def test_every_episode_selects_every_settled_episode():
    assert selected(EveryEpisodeSchedule(), target=4) == [1, 2, 3, 4]


def test_every_five_can_include_first_and_final():
    schedule = EveryNEpisodesSchedule(
        step=5,
        include_first=True,
        include_final=True,
    )
    assert selected(schedule, target=12) == [1, 5, 10, 12]


def test_explicit_schedule_resolves_last():
    schedule = ExplicitEpisodesSchedule(frozenset({1, 5, 20}))
    assert selected(schedule, target=20) == [1, 5, 20]


def test_parser_normalizes_supported_forms():
    assert selected(parse_family_schedule("every", 10), 10) == list(range(1, 11))
    assert selected(parse_family_schedule("every:5", 20), 20) == [1, 5, 10, 15, 20]
    assert selected(parse_family_schedule("1,every:5,last", 20), 20) == [1, 5, 10, 15, 20]
    assert selected(parse_family_schedule("1,7,last", 10), 10) == [1, 7, 10]
