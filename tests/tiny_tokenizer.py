"""A small real byte-level BPE tokenizer trained on synthetic text.

Gives tests a genuine fast tokenizer (offsets, truncation state, batch calls)
without a model download or the network.
"""
import random
import string


def build_tokenizer(model_max_length: int | None = None):
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers
    from transformers import PreTrainedTokenizerFast

    rng = random.Random(1)
    words = ["".join(rng.choices(string.ascii_lowercase, k=rng.randint(2, 9))) for _ in range(300)]
    corpus = [" ".join(rng.choices(words, k=40)) + " https://example.test/a/b?id=12345 | 3.14 |" for _ in range(300)]
    tk = Tokenizer(models.BPE())
    tk.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tk.decoder = decoders.ByteLevel()
    trainer = trainers.BpeTrainer(vocab_size=700, special_tokens=["<pad>"], initial_alphabet=pre_tokenizers.ByteLevel.alphabet())
    tk.train_from_iterator(corpus, trainer)
    kwargs = {"model_max_length": model_max_length} if model_max_length else {}
    return PreTrainedTokenizerFast(tokenizer_object=tk, pad_token="<pad>", **kwargs)


def text_with_tokens(tokenizer, n: int, seed: int = 11) -> str:
    """Random lowercase text that ``tokenizer`` encodes to exactly ``n`` tokens."""
    rng = random.Random(seed)
    text = ""
    while len(tokenizer(text, add_special_tokens=False)["input_ids"]) < n:
        text += rng.choice(string.ascii_lowercase)
    assert len(tokenizer(text, add_special_tokens=False)["input_ids"]) == n
    return text
