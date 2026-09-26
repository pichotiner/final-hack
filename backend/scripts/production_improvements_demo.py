"""Run PI-01 synthetic examples; print a report, never change production state.

From backend: python -m scripts.production_improvements_demo
"""

import argparse
import html
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.production_improvements import (
    AlarmEvent,
    Health,
    Installation,
    Link,
    Method,
    Run,
    Sample,
    Task,
    accept_handoff,
    alarm_projection,
    early_warning,
    lot_exposure,
    next_checks,
    prepare_handoff,
    readiness,
    signal_exposure,
    stage_map,
    tool_passport,
)


def demonstrate():
    now = datetime(2026, 9, 26, 10, tzinfo=UTC)
    key = ("sensor", "M1", "vibration", "rule-v1", "run-B")
    healthy = Health("health-1", "post-1", now, now + timedelta(minutes=5), *([True] * 6))
    run = Run("run-B", "B", "M1", "slot1", now, now + timedelta(minutes=3), "steel", "P1")
    install = Installation("install-1", "tool-new", "M1", "slot1", now, run.end)
    method = Method(
        "ultrasound",
        "1",
        "steel",
        "inside",
        True,
        (("material", "echo"), ("assembly", "normal"), ("geometry", "normal")),
        10,
    )
    tasks = [
        Task("command", 1, "unknown_command", "master-1", now),
        Task("alarm", 1, "alarm", "master-1", now),
        Task("delivery", 1, "delivery_pending", "master-1", now),
    ]
    handoff = prepare_handoff("handoff-1", tasks, "master-1", "master-2", now)
    alarm = AlarmEvent("event-1", "warning-1", key, "raise", now)
    other = AlarmEvent("event-2", "warning-2", (*key[:2], "coolant", *key[3:]), "raise", now)
    ack = AlarmEvent("ack-1", "warning-1", key, "acknowledge", now, "operator-1")
    signals = [
        Sample(f"window-{i}", now + timedelta(seconds=i), x, True, key)
        for i, x in enumerate([1.1, 1.3, 1.5, 1.6])
    ]
    before_run = replace(run, item="A", run_id="run-A", start=now - timedelta(minutes=3), end=now)
    foreign = replace(run, item="C", run_id="run-C", equipment="M2")
    row = {
        "cohort": "steel/P1/v1",
        "stage": "machining",
        "item": "A",
        "assessable": True,
        "detected": True,
        "origin_established": False,
    }
    result = {
        "status": "SYNTHETIC_RULES_EXPERIMENT_NOT_RUNTIME",
        "excluded": "improvement_7_shadow_analyzer",
        "self_diagnosis": {
            "ready": readiness(healthy, now),
            "covered_camera": readiness(replace(healthy, focus=False), now),
            "expired": readiness(healthy, now + timedelta(minutes=5)),
        },
        "tool_passport": tool_passport(
            run,
            [install],
            history_complete=True,
            profile_key=("M1", "slot1", "tool-old", "steel", "P1"),
        ),
        "lot_quarantine_proposal": lot_exposure(
            "lot-1",
            ("part",),
            [Link("assembly-1", "part", "A", True), Link("assembly-2", "A", "B", False)],
            ("part", "A", "B", "C"),
        ),
        "next_check": next_checks(
            ("material", "assembly", "geometry"), [method], material="steel", region="inside"
        ),
        "handoff": accept_handoff(handoff, tasks, actor="master-2", now=now + timedelta(minutes=1)),
        "alarms_before_ack": alarm_projection([alarm] * 100 + [other], now),
        "alarms_after_ack": alarm_projection([alarm, other, ack], now),
        "offline_monitoring": alarm_projection([alarm], now, monitoring_available=False),
        "ewma": early_warning(signals, expected_key=key, baseline=1, alpha=0.3, threshold=1.2),
        "exposure_before": signal_exposure(
            "M1", now, now + timedelta(minutes=1), [before_run, run, foreign]
        ),
        "exposure_after": signal_exposure(
            "M1", now - timedelta(minutes=1), now + timedelta(minutes=1), [before_run, run, foreign]
        ),
        "stage_map": stage_map(
            [row, row, {**row, "item": "B", "assessable": False, "detected": False}]
        ),
    }
    return result


def preview(report):
    payload = json.dumps(report, ensure_ascii=False).replace("<", "\\u003c")
    sections = [
        ("Готовность поста", "self_diagnosis"),
        ("Паспорт инструмента", "tool_passport"),
        ("Предложение карантина партии", "lot_quarantine_proposal"),
        ("Следующий полезный контроль", "next_check"),
        ("Передача смены", "handoff"),
        ("Раннее предупреждение EWMA", "ewma"),
        ("Охват до позднего сигнала", "exposure_before"),
        ("Охват после позднего сигнала", "exposure_after"),
        ("Карта этапов", "stage_map"),
    ]
    cards = "".join(
        f"<section><h2>{title}</h2><pre>"
        f"{html.escape(json.dumps(report[key], ensure_ascii=False, indent=2))}</pre></section>"
        for title, key in sections
    )
    return (
        """<!doctype html><html lang="ru"><meta charset="utf-8">
<title>PI-01 — синтетический эксперимент</title>
<style>body{font:17px system-ui;max-width:1100px;margin:30px auto;padding:16px;color:#142b45}
section{border:1px solid #aac1d2;padding:18px;margin:18px 0;border-radius:10px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}button{padding:12px;margin:5px}
.lamp{display:inline-block;width:22px;height:22px;border-radius:50%;background:#d29000;margin-right:10px}
.unavailable{background:#727d87}.notice{background:#fff0cc;padding:16px}</style>
<h1>Улучшения 1–6: исполняемый пример</h1>
<p class="notice">СИНТЕТИЧЕСКИЙ ЭКСПЕРИМЕНТ. Результаты чистых правил, без производственной БД,
назначения карантина или передачи реальной смены. Свет и звук ниже — браузерная имитация.
Пункт 7 исключён. Пункт 8 подключён отдельно в основном UI к настоящим B.</p>
<section><h2>Тревоги и сигнализация</h2>
<p id="state" role="status"></p>
<button id="sound">Разрешить короткий звук текущих эпизодов</button>
<button id="ack">Увидел warning-1 / заглушить</button>
<button id="offline">Потерять связь мониторинга</button>
<p>100 дублей дают один эпизод вибрации; охлаждение — отдельная проблема.
ACK первого не закрывает ни один эпизод. Звук разрешается только кнопкой.
Память услышанных эпизодов хранится лишь до закрытия страницы.</p><pre id="alarm"></pre></section>
"""
        + cards
        + """<script>
const report = """
        + payload
        + """;
let state=report.alarms_before_ack, unavailable=false;
const heard=new Set();
function draw(){
document.getElementById('state').replaceChildren();
const lamp=document.createElement('span');lamp.className='lamp'+(unavailable?' unavailable':'');
document.getElementById('state').append(lamp,unavailable
?'Мониторинг недоступен; проблемы остаются открытыми'
:'Жёлтый: требуется проверка, это не подтверждённый брак');
document.getElementById('alarm').textContent=JSON.stringify(state,null,2);
}
document.getElementById('ack').onclick=()=>{state=report.alarms_after_ack;if(unavailable)state={...state,light:'unavailable',sound_tokens:[]};draw();};
document.getElementById('offline').onclick=()=>{unavailable=true;state={...state,light:'unavailable',sound_tokens:[]};draw();};
document.getElementById('sound').onclick=async()=>{
const fresh=state.sound_tokens.filter(x=>!heard.has(x));if(!fresh.length)return;
try {const Audio=window.AudioContext||window.webkitAudioContext;
if(!Audio)throw new Error('Звук не поддерживается');
const audio=new Audio();await audio.resume();
fresh.forEach((id,i)=>{if(!state.sound_tokens.includes(id))return;heard.add(id);
const osc=audio.createOscillator(),gain=audio.createGain();
osc.frequency.value=520+i*120;gain.gain.value=.04;
osc.connect(gain);gain.connect(audio.destination);osc.start(audio.currentTime+i*.25);osc.stop(audio.currentTime+i*.25+.12);});
setTimeout(()=>audio.close(),1000);
} catch(e){document.getElementById('state').append(' Звук недоступен: '+e.message);}
};draw();
</script></html>"""
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", type=Path, help="Create a new standalone synthetic preview file")
    args = parser.parse_args()
    result = demonstrate()
    if args.html:
        # Exclusive creation protects an existing user file from overwrite.
        with args.html.open("x", encoding="utf-8") as stream:
            stream.write(preview(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))
