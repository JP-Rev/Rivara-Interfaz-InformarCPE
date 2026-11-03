#!/usr/bin/env python3
"""Hace que la app se olvide de una CPE, para poder volver a informarla.

La app no informa dos veces la misma CPE: lo que quedo como "informada" o
"ya_estaba" en estado.json se saltea para siempre. Eso es lo que se quiere casi
siempre, pero no cuando una CPE se anula y hay que volver a cargarla, o cuando
se cargo con el participante equivocado.

Esta herramienta borra esas anotaciones. NO toca nada en BolsaTech: solo hace
que la proxima vez la app vuelva a intentar. Si del otro lado la CPE sigue
existiendo, el alta va a rebotar igual y volvera a quedar como "ya_estaba",
que es la red de seguridad real.

Antes de cambiar nada deja un respaldo de estado.json con la fecha en el
nombre, asi volver atras es copiar ese archivo encima.

    docker compose exec informarcpe python -m herramientas.olvidar_cpe 10130966598
    docker compose exec informarcpe python -m herramientas.olvidar_cpe --confirmar 10130966598
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime

from app import bolsatech


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ctgs", nargs="+", help="los CTG a olvidar")
    parser.add_argument("--confirmar", action="store_true",
                        help="borrar de verdad; sin esto solo muestra que haria")
    args = parser.parse_args()

    ctgs = [re.sub(r"\D", "", c) for c in args.ctgs]
    malos = [c for c in ctgs if len(c) != 11]
    if malos:
        print("No son CTG de 11 digitos: " + ", ".join(malos), file=sys.stderr)
        return 1

    datos = bolsatech.estado()
    estan = [c for c in ctgs if c in datos]
    faltan = [c for c in ctgs if c not in datos]

    for c in faltan:
        print(f"  {c}  no esta anotado: la app ya lo volveria a informar")
    for c in estan:
        d = datos[c]
        print(f"  {c}  {d.get('estado')}  track={d.get('track_id') or '-'}  "
              f"{d.get('cuando') or ''}")

    if not estan:
        print("\nNo hay nada que olvidar.")
        return 0

    if not args.confirmar:
        print(f"\nModo ver: no se borro nada. Agregar --confirmar para olvidar "
              f"{len(estan)} CPE.")
        return 0

    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    respaldo = bolsatech.ESTADO.with_name(f"estado_{marca}.json")
    try:
        shutil.copy2(bolsatech.ESTADO, respaldo)
        print(f"\nRespaldo en {respaldo}")
    except OSError as error:
        print(f"No se pudo dejar el respaldo ({error}); no se borra nada.",
              file=sys.stderr)
        return 1

    for c in estan:
        del datos[c]
    bolsatech._guardar_estado(datos)
    print(f"Olvidadas {len(estan)} CPE. La proxima vez se vuelven a intentar.")
    print("Para volver atras: cp " + str(respaldo) + " " + str(bolsatech.ESTADO))
    return 0


if __name__ == "__main__":
    sys.exit(main())
