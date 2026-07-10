from plugin.plugins.neko_roast.core import live_hosting_director
from plugin.plugins.neko_roast.core.live_hosting_beat_rules import (
    idle_hosting_beat_candidates,
)
from plugin.plugins.neko_roast.core.live_material_rules import (
    is_clean_live_material,
    is_similar_live_material_title,
)
from plugin.plugins.neko_roast.modules.active_engagement import ActiveEngagementModule
from plugin.plugins.neko_roast.modules.warmup_hosting import WarmupHostingModule


def test_hosting_modules_import_without_active_topic_slice():
    assert ActiveEngagementModule.id == "active_engagement"
    assert WarmupHostingModule.id == "warmup_hosting"
    assert live_hosting_director.LiveHostingDirector is not None
    assert idle_hosting_beat_candidates() == []


def test_live_material_safety_rejects_unsafe_or_malformed_text():
    assert is_clean_live_material({"title": "A tiny room callback"})
    assert not is_clean_live_material({"title": "nuclear reactor tutorial"})
    assert not is_clean_live_material({"title": 'broken "quote'})
    assert not is_clean_live_material({})


def test_live_material_title_similarity_handles_duplicates_and_variants():
    recent = ["Tonight's tiny question"]

    assert is_similar_live_material_title("Tonight tiny question", recent)
    assert not is_similar_live_material_title("Completely different topic", recent)
