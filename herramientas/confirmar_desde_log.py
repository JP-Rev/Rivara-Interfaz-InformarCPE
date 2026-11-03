#!/usr/bin/env python3
"""Confirma en BolsaTech los trackId que quedaron pendientes en los logs viejos.

Es una herramienta de una sola vez: los scripts anteriores
(cpe_bolsatech.py y cpe_bolsatech_desde_csv.py) corrian sin
BOLSATECH_TRACK_URL, asi que cada alta quedaba con su trackId
"REGISTRADA_PENDIENTE_CONFIRMACION" y nunca se acuso recibo. Lo unico que
queda de esas corridas son los .log, asi que de ahi se saca el trackId.

Confirmar un trackId NO da de alta ninguna CPE: solo le dice a BolsaTech que
se recibio el acuse. Volver a confirmar uno ya confirmado tampoco agrega nada.

Como se corre, dentro del contenedor:

    # 1. Dejar los .log donde el contenedor los vea
    sudo mkdir -p /storage/informarcpe/logs
    sudo cp ejecuciones.log ejecuciones_desde_csv.log /storage/informarcpe/logs/

    # 2. Ver que haria, sin enviar nada
    docker compose exec informarcpe python -m herramientas.confirmar_desde_log \
        /data/logs/ejecuciones.log /data/logs/ejecuciones_desde_csv.log

    # 3. Confirmar de verdad (se puede cortar y volver a correr: retoma)
    docker compose exec informarcpe python -m herramientas.confirmar_desde_log \
        --confirmar /data/logs/*.log

El avance se guarda en /data/bolsatech/confirmacion_log.json despues de cada
trackId, asi que si se corta la conexion o se reinicia el contenedor, volver a
correrlo sigue donde quedo y no repite lo que ya salio bien.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from app import bolsatech
from app.config import config

# "2026-08-03 14:49:19 | BOLSATECH | Procesando 1/46 | Carta=600002614 | CTG=10123752898"
PROCESANDO = re.compile(r"Procesando \d+/\d+ \| Carta=(\S+) \| CTG=(\d{11})")
# "... | REGISTRO | REGISTRADA_PENDIENTE_CONFIRMACION | TrackID=96682719"
PENDIENTE = re.compile(r"REGISTRADA_PENDIENTE_CONFIRMACION \| TrackID=(\d+)")
# "... | CREDENCIALES | Carta=100005961 | CTG=10134032950 | IdMuestra=779... | Cuenta=MLIAUDAT"
CREDENCIALES = re.compile(
    r"CREDENCIALES \| Carta=(\S+) \| CTG=(\d{11}) \| IdMuestra=(\S+) \| Cuenta=(\S+)")

AVANCE = bolsatech.BOLSATECH_DIR / "confirmacion_log.json"


def leer_logs(rutas: list[Path]) -> tuple[list[dict], dict[str, dict]]:
    """Devuelve (pendientes, datos por CTG). Un pendiente por trackId."""
    pendientes: list[dict] = []
    vistos: set[str] = set()
    por_ctg: dict[str, dict] = {}
    for ruta in rutas:
        ctg = carta = ""
        with ruta.open(encoding="utf-8", errors="replace") as archivo:
            for linea in archivo:
                cred = CREDENCIALES.search(linea)
                if cred:
                    _, ctg_c, muestra, cuenta = cred.groups()
                    por_ctg[ctg_c] = {"id_muestra": muestra, "cuenta": cuenta.lower()}
                    continue
                proc = PROCESANDO.search(linea)
                if proc:
                    carta, ctg = proc.groups()
                    continue
                pend = PENDIENTE.search(linea)
                if pend and ctg:
                    track = pend.group(1)
                    if track in vistos:   # el mismo trackId repetido en dos logs
                        continue
                    vistos.add(track)
                    pendientes.append({
                        "track_id": track, "ctg": ctg,
                        "numero_carta_porte": carta,
                        "cuando": linea[:19], "log": ruta.name,
                    })
    pendientes.sort(key=lambda p: p["cuando"])
    return pendientes, por_ctg


def cuentas_desde_oracle(ctgs: list[str]) -> dict[str, dict]:
    """El id_muestra que tiene el ERP, para los CTG que el log no aclara."""
    from app import oracle

    salida: dict[str, dict] = {}
    for desde in range(0, len(ctgs), 200):      # de a 200, por el IN de Oracle
        for fila in oracle.buscar_bolsatech(ctgs[desde:desde + 200]):
            muestra = (fila.get("ID_MUESTRA") or "").strip()
            if muestra:
                salida[str(fila["IPL_CTG"]).strip()] = {
                    "id_muestra": muestra,
                    "cuenta": bolsatech._cuenta_de(muestra),
                }
    return salida


def leer_avance() -> dict[str, dict]:
    if not AVANCE.is_file():
        return {}
    try:
        return json.loads(AVANCE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"Aviso: {AVANCE} ilegible; se arranca de cero.", file=sys.stderr)
        return {}


def guardar_avance(avance: dict) -> None:
    AVANCE.parent.mkdir(parents=True, exist_ok=True)
    temporal = AVANCE.with_suffix(".tmp")
    temporal.write_text(json.dumps(avance, ensure_ascii=False, indent=1, sort_keys=True),
                        encoding="utf-8")
    temporal.replace(AVANCE)


def _cpe(p: dict, cuenta: str = "") -> bolsatech.Cpe:
    """Para confirmar solo hacen falta el trackId y con que cuenta se firma."""
    return bolsatech.Cpe(
        ctg=p["ctg"], numero_carta_porte=p.get("numero_carta_porte", ""),
        cuit_productor="", productor="", especie="", establecimiento="",
        localidad_procedencia="", planta_oncca="", localidad_destino="",
        id_muestra=p.get("id_muestra", ""), kilos="", fecha="",
    )


def confirmar_uno(p: dict, cuentas: list[str]) -> tuple[bool, str, str]:
    """Prueba con cada cuenta hasta que una confirme. (ok, cuenta, detalle)."""
    ultimo = ""
    for cuenta in cuentas:
        cpe = _cpe(p, cuenta)
        try:
            if bolsatech.confirmar(cpe, p["track_id"], cuenta):
                return True, cuenta, ""
            ultimo = "BolsaTech no devolvio ese trackId entre los confirmados"
        except bolsatech.ErrorBolsaTech as error:
            ultimo = str(error)
    return False, "", ultimo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logs", nargs="+", type=Path, help="los .log a leer")
    parser.add_argument("--confirmar", action="store_true",
                        help="enviar de verdad; sin esto solo muestra que haria")
    parser.add_argument("--limite", type=int, default=0,
                        help="cuantos trackId como maximo en esta corrida (0 = todos)")
    parser.add_argument("--espera", type=float, default=0.3,
                        help="segundos entre llamadas, para no ahogar el servicio")
    parser.add_argument("--sin-oracle", action="store_true",
                        help="no consultar el ERP para averiguar el id_muestra")
    args = parser.parse_args()

    faltan = [r for r in args.logs if not r.is_file()]
    if faltan:
        print("No existen estos archivos: " + ", ".join(str(r) for r in faltan),
              file=sys.stderr)
        return 1

    pendientes, por_ctg = leer_logs(args.logs)
    if not pendientes:
        print("En esos logs no hay ningun trackId pendiente de confirmacion.")
        return 0

    avance = leer_avance()
    quedan = [p for p in pendientes if not avance.get(p["track_id"], {}).get("ok")]
    ctgs = sorted({p["ctg"] for p in pendientes})

    print(f"trackId pendientes en los logs: {len(pendientes)} "
          f"({len(ctgs)} CTG distintos)")
    ya_ok = len(pendientes) - len(quedan)
    if ya_ok:
        print(f"ya confirmados en una corrida anterior: {ya_ok}")
    print(f"a confirmar ahora: {len(quedan)}")

    repetidos = len(pendientes) - len(ctgs)
    if repetidos > 0:
        print(f"\nOjo: hay {repetidos} trackId de mas respecto de los CTG, o sea "
              f"CTG que se registraron mas de una vez.\nConfirmar no duplica nada, "
              f"pero esas CPE pueden haber quedado duplicadas en BolsaTech del\n"
              f"alta misma. Eso se revisa aparte, con BolsaTech.")

    sin_cuenta = [c for c in ctgs if c not in por_ctg]
    if sin_cuenta and not args.sin_oracle:
        print(f"\nCTG sin cuenta en el log: {len(sin_cuenta)}. "
              f"Buscando el id_muestra en el ERP...")
        try:
            por_ctg.update(cuentas_desde_oracle(sin_cuenta))
        except Exception as error:      # el ERP no es imprescindible
            print(f"No se pudo consultar el ERP ({error}). "
                  f"Para esos CTG se prueban las dos cuentas.", file=sys.stderr)
    sin_cuenta = [c for c in ctgs if c not in por_ctg]
    if sin_cuenta:
        print(f"CTG para los que se van a probar las dos cuentas: {len(sin_cuenta)}")

    if args.limite > 0:
        quedan = quedan[:args.limite]

    if not args.confirmar:
        print(f"\nModo ver: no se envio nada. Agregar --confirmar para hacerlo.")
        for p in quedan[:10]:
            datos = por_ctg.get(p["ctg"], {})
            print(f"  {p['cuando']}  CTG {p['ctg']}  trackId {p['track_id']}  "
                  f"cuenta {datos.get('cuenta', '(a probar)')}")
        if len(quedan) > 10:
            print(f"  ... y {len(quedan) - 10} mas")
        return 0

    if not config.bolsatech_url_track:
        print("Falta BOLSATECH_TRACK_URL en el .env: sin eso no se puede confirmar.",
              file=sys.stderr)
        return 1
    if config.problemas_bolsatech():
        print("Falta configurar BolsaTech: " + "; ".join(config.problemas_bolsatech()),
              file=sys.stderr)
        return 1

    ok = fallados = 0
    confirmados_por_ctg: set[str] = {
        d["ctg"] for d in avance.values() if d.get("ok")
    }
    try:
        for numero, p in enumerate(quedan, 1):
            datos = por_ctg.get(p["ctg"], {})
            p["id_muestra"] = datos.get("id_muestra", "")
            cuentas = [datos["cuenta"]] if datos.get("cuenta") else ["rivara", "mliaudat"]
            logro, cuenta, detalle = confirmar_uno(p, cuentas)
            avance[p["track_id"]] = {
                "ctg": p["ctg"], "ok": logro, "cuenta": cuenta,
                "detalle": detalle, "log": p["log"],
                "cuando": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            guardar_avance(avance)
            if logro:
                ok += 1
                if p["ctg"] not in confirmados_por_ctg:
                    bolsatech.anotar(_cpe(p, cuenta), bolsatech.INFORMADA, p["track_id"])
                    confirmados_por_ctg.add(p["ctg"])
            else:
                fallados += 1
            estado = "ok" if logro else f"FALLO: {detalle}"
            print(f"{numero}/{len(quedan)}  CTG {p['ctg']}  trackId {p['track_id']}  {estado}")
            if args.espera > 0:
                time.sleep(args.espera)
    except KeyboardInterrupt:
        print("\nCortado a mano. El avance quedo guardado: volver a correrlo retoma.")

    print(f"\nConfirmados: {ok}. Fallados: {fallados}. "
          f"Avance en {AVANCE}")
    if fallados:
        print("Los fallados se pueden reintentar volviendo a correr el comando: "
              "solo se reintenta lo que no salio bien.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
