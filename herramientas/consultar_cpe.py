#!/usr/bin/env python3
"""Consulta en BolsaTech el estado de una CPE, y muestra lo que contesta.

Solo lee: ninguna de estas llamadas registra, modifica ni anula nada.

El servicio identifica la carta de porte con "numeroCartaPorte|ctg", asi que
una consulta falla tanto si la CPE no esta como si esta con OTRO numero de
carta de porte. Por eso prueba tres formas y muestra el resultado de cada una:

    numero|ctg   lo que usa la app para decidir si ya esta informada
    numero       solo el numero de carta de porte
    ctg          solo el CTG

Si la segunda o la tercera encuentran algo que la primera no, la CPE esta en
BolsaTech con un numero distinto del que arma el ERP, y eso explica que no se
la vea buscando por el nuestro.

De cada CPE encontrada muestra el estado (INGRESADA, ANULADA, ...) y la fecha
de registracion, que es lo que el documento tecnico de Bolsatech documenta
para consultarCartasDePorte.

    docker compose exec informarcpe python -m herramientas.consultar_cpe 10130966598
    docker compose exec informarcpe python -m herramientas.consultar_cpe --xml 10130966598
"""

from __future__ import annotations

import argparse
import sys
from xml.sax.saxutils import escape

from app import bolsatech, oracle
from app.config import config

SOAP12 = bolsatech.SOAP12
NS_CPE = bolsatech.NS_CPE
NS_WSSE = bolsatech.NS_WSSE


def xml_consulta(identificador: str, cuenta: str) -> str:
    """Igual que bolsatech.xml_consulta, pero con el identificador a eleccion."""
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{SOAP12}" xmlns:t="{NS_CPE}" xmlns:wsse="{NS_WSSE}">'
            f"<s:Header>{bolsatech._seguridad(cuenta, 's')}</s:Header><s:Body>"
            "<t:consultarCartasDePorte><parametros><numerosCartaPorte>"
            f"<numeroCartaPorte>{escape(identificador)}</numeroCartaPorte>"
            "</numerosCartaPorte></parametros></t:consultarCartasDePorte>"
            "</s:Body></s:Envelope>")


def consultar(identificador: str, cuenta: str) -> tuple[list[dict], list[str], str]:
    """(cartas encontradas, errores del servicio, xml crudo de la respuesta)."""
    xml = xml_consulta(identificador, cuenta)
    respuesta = bolsatech._enviar(config.bolsatech_url, xml,
                                  "consultarCartasDePorte", True)
    raiz = bolsatech._raiz(respuesta)
    bolsatech._revisar_fault(raiz)
    cartas = []
    for e in bolsatech._todos(raiz, "cartaPorteTransporteAutomotor"):
        cartas.append({
            "numero": bolsatech._valor(e, "numeroCartaPorte"),
            "ctg": bolsatech._valor(e, "ctg"),
            "estado": bolsatech._valor(e, "estado"),
            "registrada": bolsatech._valor(e, "fechaHoraRegistracion"),
        })
    errores = []
    for e in bolsatech._todos(raiz, "error"):
        descripcion = bolsatech._valor(e, "descripcion")
        if descripcion and descripcion not in errores:
            errores.append(descripcion)
    return cartas, errores, respuesta


def datos_del_ctg(ctg: str) -> tuple[str, str]:
    """(numeroCartaPorte, cuenta) segun el ERP. Vacios si no esta."""
    try:
        filas = oracle.buscar_bolsatech([ctg])
    except Exception as error:
        print(f"  (no se pudo consultar el ERP: {error})", file=sys.stderr)
        return "", "rivara"
    if not filas:
        return "", "rivara"
    cpe = bolsatech.armar(filas[0])
    return cpe.numero_carta_porte, cpe.cuenta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ctgs", nargs="+", help="los CTG a consultar")
    parser.add_argument("--numero", default="",
                        help="forzar el numero de carta de porte (un solo CTG)")
    parser.add_argument("--cuenta", choices=["rivara", "mliaudat"], default="",
                        help="forzar con que credenciales consultar")
    parser.add_argument("--xml", action="store_true",
                        help="mostrar tambien el XML crudo de cada respuesta")
    args = parser.parse_args()

    if config.problemas_bolsatech():
        print("Falta configurar BolsaTech: " + "; ".join(config.problemas_bolsatech()),
              file=sys.stderr)
        return 1

    for ctg in args.ctgs:
        print(f"\n===== CTG {ctg} " + "=" * 40)
        numero, cuenta = (args.numero, args.cuenta or "rivara")
        if not numero:
            numero, cuenta_erp = datos_del_ctg(ctg)
            cuenta = args.cuenta or cuenta_erp
        if not numero:
            print("  El ERP no tiene este CTG; solo se prueba por CTG.")
        print(f"  numero de carta de porte segun el ERP: {numero or '(no esta)'}")
        print(f"  cuenta: {cuenta}")

        formas = [("numero|ctg", f"{numero}|{ctg}")] if numero else []
        if numero:
            formas.append(("solo el numero", numero))
        formas.append(("solo el ctg", ctg))

        for etiqueta, identificador in formas:
            print(f"\n  --- consultando por {etiqueta}: {identificador}")
            try:
                cartas, errores, crudo = consultar(identificador, cuenta)
            except bolsatech.ErrorBolsaTech as error:
                print(f"      ERROR: {error}")
                continue
            if cartas:
                for c in cartas:
                    print(f"      ENCONTRADA  numero={c['numero']}  ctg={c['ctg']}  "
                          f"estado={c['estado'] or '(sin estado)'}  "
                          f"registrada={c['registrada'] or '-'}")
            else:
                print("      no la encontro" +
                      (f" — {'; '.join(errores)}" if errores else ""))
            if args.xml:
                print("      XML:")
                for linea in crudo.splitlines():
                    print("        " + linea)
    return 0


if __name__ == "__main__":
    sys.exit(main())
