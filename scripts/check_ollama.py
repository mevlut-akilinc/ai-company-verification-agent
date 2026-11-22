"""Ollama bağlantı testi: sunucuya ulaşılıyor mu, model cevap veriyor mu, hız ne?"""

import sys

import ollama

MODEL = "qwen3:4b-instruct"  # `ollama list` çıktısındaki adla birebir aynı olmalı


def main() -> int:
    # 1) Sunucuya ulaşılıyor mu? Yerel modelleri listele.
    try:
        models = ollama.list()
    except ConnectionError as e:
        print(f"Ollama sunucusuna ulaşılamadı: {e}")
        print("Ollama uygulamasının açık olduğundan emin ol.")
        return 1

    print("Yerel modeller:")
    for m in models.models:
        print(f"  - {m.model}")

    # 2) Modele tek bir mesaj gönder.
    try:
        response = ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": "Merhaba, tek cümleyle kendini tanıt."}],
            options={"num_predict": 200},
        )
    except ollama.ResponseError as e:
        print(f"Model hatası ({e.status_code}): {e.error}")
        if e.status_code == 404:
            print(f"'{MODEL}' bulunamadı. Yukarıdaki listeden doğru adı kopyala.")
        return 1

    print("\nCevap:")
    print(response.message.content)

    # 3) Token sayıları ve hız (süreler nanosaniye cinsinden gelir).
    prompt_tokens = response.prompt_eval_count or 0
    gen_tokens = response.eval_count or 0
    prompt_sec = (response.prompt_eval_duration or 0) / 1e9
    gen_sec = (response.eval_duration or 0) / 1e9
    load_sec = (response.load_duration or 0) / 1e9

    print("\nÖlçümler:")
    print(f"  Model yükleme : {load_sec:.2f} s")
    if prompt_sec > 0:
        print(f"  Okuma         : {prompt_tokens} token, {prompt_tokens / prompt_sec:.1f} tok/s")
    if gen_sec > 0:
        print(f"  Üretim        : {gen_tokens} token, {gen_tokens / gen_sec:.1f} tok/s")
    print(f"  Bitiş sebebi  : {response.done_reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
