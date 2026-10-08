import json

import numpy as np

from src.matcher import semantic


class _FakeEncoder:
    def __init__(self):
        self.calls = []

    def encode(self, texts, **_kwargs):
        self.calls.append(list(texts))
        return np.asarray([[index + 1, 2, 3] for index, _ in enumerate(texts)], dtype=np.float32)

    @staticmethod
    def get_sentence_embedding_dimension():
        return 3


def _source(profiles):
    return json.dumps(profiles, sort_keys=True).encode("utf-8")


def test_profile_vectors_are_cached_by_model_and_exact_profile_bytes(tmp_path):
    model = _FakeEncoder()
    profiles = {"Wet Lab": {"text": "qPCR and western blot"}, "Bioinformatics": {"text": "NGS analysis"}}
    source = _source(profiles)

    first = semantic._load_or_encode_profile_vectors(model, profiles, source, cache_dir=tmp_path)
    second = semantic._load_or_encode_profile_vectors(model, profiles, source, cache_dir=tmp_path)

    assert len(model.calls) == 1
    np.testing.assert_array_equal(first, second)
    assert list(tmp_path.glob("semantic_profiles_*.npz"))


def test_profile_cache_invalidates_when_profile_bytes_change(tmp_path):
    model = _FakeEncoder()
    profiles_v1 = {"Wet Lab": {"text": "qPCR"}}
    profiles_v2 = {"Wet Lab": {"text": "qPCR and western blot"}}

    semantic._load_or_encode_profile_vectors(model, profiles_v1, _source(profiles_v1), cache_dir=tmp_path)
    semantic._load_or_encode_profile_vectors(model, profiles_v2, _source(profiles_v2), cache_dir=tmp_path)

    assert len(model.calls) == 2
    assert len(list(tmp_path.glob("semantic_profiles_*.npz"))) == 2


def test_corrupt_or_shape_invalid_cache_recomputes_safely(tmp_path):
    model = _FakeEncoder()
    profiles = {"Wet Lab": {"text": "qPCR"}}
    source = _source(profiles)
    cache_path = tmp_path / f"semantic_profiles_{semantic._profile_cache_key(source)}.npz"
    cache_path.write_bytes(b"broken archive")

    vectors = semantic._load_or_encode_profile_vectors(model, profiles, source, cache_dir=tmp_path)

    assert len(model.calls) == 1
    assert vectors.shape == (1, 3)
    with np.load(cache_path, allow_pickle=False) as saved:
        assert saved["vectors"].shape == (1, 3)
        assert saved["schema"].item() == semantic.PROFILE_CACHE_SCHEMA
    assert not list(tmp_path.glob("*.tmp"))


def test_wrong_embedding_dimension_cache_is_recomputed(tmp_path):
    model = _FakeEncoder()
    profiles = {"Wet Lab": {"text": "qPCR"}}
    source = _source(profiles)
    cache_key = semantic._profile_cache_key(source)
    cache_path = tmp_path / f"semantic_profiles_{cache_key}.npz"
    np.savez_compressed(
        cache_path,
        schema=np.asarray(semantic.PROFILE_CACHE_SCHEMA, dtype=np.int64),
        cache_key=np.asarray(cache_key),
        profile_names=np.asarray(list(profiles), dtype=str),
        vectors=np.ones((1, 2), dtype=np.float32),
    )

    vectors = semantic._load_or_encode_profile_vectors(model, profiles, source, cache_dir=tmp_path)

    assert len(model.calls) == 1
    assert vectors.shape == (1, 3)
