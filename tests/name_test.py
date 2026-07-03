"""Unit-Tests für die Prompt-Namens-Extraktion (extract_display_name).

Deckt die gängigen Workflow-Formen ab — insbesondere den Bug, dass bei
längerem Negativ-Prompt-Boilerplate alle Jobs denselben Text anzeigten.

Läuft ohne Server:  .venv/bin/python tests/name_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.queue_logic import extract_display_name  # noqa: E402

NEG = ("worst quality, inconsistent motion, blurry, jittery, distorted, "
       "watermark, text, logo, deformed hands, extra fingers, low quality")

passed = 0


def check(name: str, prompt: dict, expected: str) -> None:
    global passed
    got = extract_display_name(prompt)
    ok = got == expected
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + ("" if ok else f"\n         erwartet: {expected!r}\n         bekommen: {got!r}"))
    if not ok:
        sys.exit(1)
    passed += 1


# 1) Klassisch (SD1.5/LTX-Stil): KSampler mit positive/negative,
#    Negativ-Prompt ist LÄNGER als der Positiv-Prompt (der alte Bug).
sd15 = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "ein rotes Auto", "clip": ["4", 1]}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG, "clip": ["4", 1]}},
    "3": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "negative": ["2", 0], "seed": 1}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd15.safetensors"}},
}
check("Positiv-Prompt gewinnt, auch wenn Negativ länger ist", sd15, "ein rotes Auto")

# 2) LTX-Video: CLIPTextEncode -> LTXVConditioning(positive, negative) -> Sampler
ltx = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "drohnenflug über einen fjord", "clip": ["9", 0]}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG, "clip": ["9", 0]}},
    "3": {"class_type": "LTXVConditioning", "inputs": {"positive": ["1", 0], "negative": ["2", 0], "frame_rate": 25}},
    "4": {"class_type": "SamplerCustom", "inputs": {"positive": ["3", 0], "negative": ["3", 1]}},
    "9": {"class_type": "CLIPLoader", "inputs": {"clip_name": "t5xxl.safetensors"}},
}
check("LTX-Video: Conditioning-Kette wird rückwärts verfolgt", ltx, "drohnenflug über einen fjord")

# 3) SDXL: text_g/text_l statt text
sdxl = {
    "1": {"class_type": "CLIPTextEncodeSDXL", "inputs": {"text_g": "portrait einer astronautin", "text_l": "portrait", "clip": ["4", 1]}},
    "2": {"class_type": "CLIPTextEncodeSDXL", "inputs": {"text_g": NEG, "text_l": NEG, "clip": ["4", 1]}},
    "3": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "negative": ["2", 0]}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
}
check("SDXL: text_g wird gefunden", sdxl, "portrait einer astronautin")

# 4) Flux/BasicGuider: kein "positive"-Eingang im ganzen Graphen
flux = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "neonlichter im regen", "clip": ["8", 0]}},
    "2": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["1", 0], "guidance": 3.5}},
    "3": {"class_type": "BasicGuider", "inputs": {"model": ["7", 0], "conditioning": ["2", 0]}},
    "7": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux1-dev.safetensors"}},
    "8": {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": "clip_l.safetensors"}},
}
check("Flux ohne positive-Eingang: Fallback findet den Text", flux, "neonlichter im regen")

# 5) Text kommt aus einem verlinkten Primitive-/String-Node
primitive = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ["7", 0], "clip": ["4", 1]}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG, "clip": ["4", 1]}},
    "3": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "negative": ["2", 0]}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd15.safetensors"}},
    "7": {"class_type": "Text Multiline", "inputs": {"text": "text aus primitive node"}},
}
check("Verlinkter Text-Input (Primitive-Node) wird aufgelöst", primitive, "text aus primitive node")

# 6) Wan-Stil: positive_prompt/negative_prompt im selben Node
wan = {
    "1": {"class_type": "WanVideoTextEncode", "inputs": {"positive_prompt": "eine welle bricht in zeitlupe", "negative_prompt": NEG}},
    "2": {"class_type": "WanVideoSampler", "inputs": {"text_embeds": ["1", 0], "steps": 30}},
}
check("Wan: positive_prompt hat Vorrang", wan, "eine welle bricht in zeitlupe")

# 7) Kein Text im Workflow: Modellname als Fallback
notext = {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "ltx-video-2b.safetensors"}},
    "2": {"class_type": "SomeImageNode", "inputs": {"image": "input.png"}},
}
check("Ohne Text: Modellname", notext, "ltx-video-2b.safetensors")

# 8) Negatives Conditioning teilt sich Nodes mit dem positiven
#    (ConditioningZeroOut wie im SD3-Template) — darf nicht alles ausschließen.
zeroout = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "kristallhöhle, volumetrisches licht", "clip": ["8", 0]}},
    "2": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["1", 0]}},
    "3": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "negative": ["2", 0]}},
    "8": {"class_type": "CLIPLoader", "inputs": {"clip_name": "t5.safetensors"}},
}
check("ConditioningZeroOut-Negativ (SD3-Stil)", zeroout, "kristallhöhle, volumetrisches licht")

print(f"\nAlle {passed} Namens-Checks bestanden ✓")
