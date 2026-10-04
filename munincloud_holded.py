#!/usr/bin/env python3
"""De la respuesta `v1` de la API de auditoría de facturas de MuninCloud al CSV de la plantilla de COMPRAS de Holded.

    python3 munincloud_holded.py --mi-nif B00000018 --cuenta-gasto 629 --salida salida/ trabajos/

Escribe en --salida (los de Holded solo si tienen alguna factura; revisar.csv y omitidos.csv, siempre):
  holded_compras.csv           para importar en Holded (Compras > Facturas > Importar): una fila por línea de la
                               factura, punto y coma, punto decimal, UTF-8 sin marca
  holded_compras_excel_es.csv  la MISMA información para abrirla en un Excel en español: coma decimal y marca UTF-8
  holded_revisar.csv           las compras que no conviene importar a ciegas, mismo formato, con [REV] en el concepto
  revisar.csv                  qué factura se apartó y por qué (las ventas también: no hay plantilla de venta)
  omitidos.csv                 lo que no era una factura terminada

Solo genera el fichero: no se conecta a Holded ni usa su API ni toca tu cuenta. Holded® es una marca de Holded;
MuninCloud no está asociado a, ni certificado por, Holded. Detalle de columnas, reglas y límites: README.md.
Solo biblioteca estándar de Python (3.8 o posterior).
"""
import argparse
import csv
import os
import re
import shutil
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import munincloud_v1 as v1  # noqa: E402

VERSION = "1.0.0"

# La plantilla de facturas de compra (help.holded.com/es/articles/7903146, leída el 2026-09-14): las diez
# obligatorias (marcadas con «*» en la ayuda) y cuatro opcionales. Mismos nombres y orden que el portal de MuninCloud.
CABECERA = ["Núm. factura", "Fecha dd/mm/yyyy", "Fecha de vencimiento dd/mm/yyyy", "Nombre del contacto", "NIF",
            "Concepto", "Precio unidad", "Unidades", "IVA %", "Cantidad cobrada",
            "Retención %", "Inv. Suj. Pasivo (1/0)", "Operación", "Núm. cuenta de gasto"]
OBLIGATORIAS = CABECERA[:10]
OPERACIONES = ("intra", "general", "impexp", "nosujeto", "exento", "receq")   # los seis valores de `Operación`
CLAVE_DOCUMENTADA = {"21": "p_iva_21"}     # la única clave que la ayuda de Holded da por escrito
NO_SUJETO = "NS"                           # la clave del fichero de impuestos para suplidos / no sujeto
LONGITUD_CUENTA = 8                        # 629 -> 62900000
_CLAVE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_SOLO_DECIMAL = re.compile(r"^-?\d+\.\d+$")
_INTRA = (v1.ADQUISICION_INTRACOMUNITARIA_BIENES, v1.SERVICIO_INTRACOMUNITARIO_ISP)
_IMPEXP = (v1.IMPORTACION_BIENES, v1.SERVICIO_EXTRACOMUNITARIO_ISP)
AVISO, REVISAR = "AVISO", "REVISAR"


# ─── lo que da el cliente ─────────────────────────────────────────────────────

def _tipo(texto):
    """'21', '9.5', 'NS' o None: el tipo de una fila del fichero de impuestos."""
    t = texto.strip().strip('"').rstrip("%").strip()
    if t.upper() == NO_SUJETO:
        return NO_SUJETO
    d = v1.decimal(t)
    return v1.texto_tipo(d) if d is not None else None


def leer_claves(ruta):
    """{'21': 'p_iva_21', '10': 'p_iva_10', 'NS': …}, del fichero «tipo;clave» con las claves de TU cuenta de Holded
    (Configuración > Facturación > Impuestos). `tipo` es el porcentaje de IVA con PUNTO decimal (21, 10, 4, 0, 9.5)
    o NS para suplidos y no sujeto. Se lee con `leer_maestro` de la pieza común; antes se aparta una cabecera propia
    y se niega lo que la pieza leería mal (una coma decimal «9,5» partiría la fila en tres columnas)."""
    if not ruta:
        return {}
    filas = []
    with open(ruta, encoding="utf-8-sig") as fh:
        for n, linea in enumerate(fh, 1):
            linea = linea.strip()
            if not linea or linea.startswith("#"):
                continue
            c = [p.strip().strip('"') for p in re.split(r"[;,\t]", linea)]
            if not filas and len(c) == 2 and _tipo(c[0]) is None:
                filas.append(None)                      # su cabecera («tipo;clave»): no cuenta
                continue
            if len(c) != 2:
                raise ValueError(f"{ruta}:{n}: se esperaban dos columnas, tipo;clave (el tipo con punto decimal: 9.5)")
            if _tipo(c[0]) is None:
                raise ValueError(f"{ruta}:{n}: el tipo «{c[0]}» no es un porcentaje ni {NO_SUJETO}")
            filas.append((n, _tipo(c[0]), c[1]))
    datos = [f for f in filas if f is not None]
    temporal = tempfile.mkdtemp(prefix="munincloud_holded_")
    try:
        plano = os.path.join(temporal, "tipo_clave.csv")
        with open(plano, "w", encoding="utf-8") as fh:
            fh.write("TIPO;clave\n")                     # la pieza la toma por cabecera: «TIPO» no tiene dígitos
            for i, f in enumerate(datos, 1):            # por número de orden: la pieza normaliza la 1.ª columna
                fh.write(f"{i};{f[2]}\n")                # como un NIF y quitaría el punto (7.5 -> 75)
        leidas = v1.leer_maestro(plano, formato=_CLAVE, que="clave de impuesto",
                                 no_valido="no es una clave de impuesto de Holded (letras, cifras y _)")
    finally:
        shutil.rmtree(temporal, ignore_errors=True)
    return {f[1]: leidas[str(i)] for i, f in enumerate(datos, 1)}


# ─── la factura en Holded ─────────────────────────────────────────────────────

def _numero(d):
    """Un Decimal como texto, sin notación científica ni ceros de más."""
    t = format(d.normalize(), "f")
    return t.rstrip("0").rstrip(".") if "." in t else t


def _fecha(iso):
    """'2026-09-25' -> '25/09/2026'; '' si no hay fecha."""
    return f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}" if iso else ""


def lineas_holded(doc, a):
    """[(concepto, unidades, precio, tipo|'NS', importe)] de una factura. Holded calcula la base de la línea como
    unidades x precio: si qty x rate no da el importe de la línea (o falta alguno), va 1 unidad y precio = importe,
    para que la base que contabiliza Holded sea la que trae la respuesta. Nunca un precio inventado a 0."""
    filas = []
    for linea in v1.lineas(doc):              # con el redondeo de 1-2 céntimos ya fundido
        linea = linea or {}
        importe = v1.decimal(linea.get("amount"))
        qty, rate = v1.decimal(linea.get("qty")), v1.decimal(linea.get("rate"))
        concepto = v1.leido(linea.get("descripcion_original")) or v1.leido(linea.get("short_name")) or "Línea sin concepto"
        tipo = NO_SUJETO if linea.get("es_no_sujeto") else v1.texto_tipo(v1.decimal(linea.get("tax_percent")) or Decimal("0"))
        if importe is None:
            filas.append((concepto, "1", "", tipo, None))
        elif qty is not None and rate is not None and qty != 0 and abs(v1.redondear(qty * rate) - v1.redondear(importe)) < v1.CENTIMO:
            filas.append((concepto, _numero(qty), _numero(rate), tipo, v1.redondear(importe)))
        else:
            filas.append((concepto, "1", v1.importe(importe), tipo, v1.redondear(importe)))
    if filas:
        return filas
    for r in a["registros"]:                      # factura sin líneas: una fila por tipo, con la base de la cabecera
        if r["clase"] == "AJUSTE":                # sin fila de ajuste: lo que no cuadra ya va a revisar
            continue
        concepto = ("Suplidos / no sujeto" if r["clase"] == "NO_SUJETO"
                    else a["primer_concepto"] or (f"Factura {a['numero']}" if a["numero"] else "Factura"))
        tipo = NO_SUJETO if r["clase"] == "NO_SUJETO" else (v1.texto_tipo(r["tipo_iva"]) if r["tipo_iva"] is not None else None)
        filas.append((concepto, "1", v1.importe(r["base"]), tipo, r["base"]))
    return filas


def operacion_factura(doc, a, mi_nif, forzada):
    """(valor de `Operación` para la factura, motivo|None). Extranjero sin clasificar: vacía y a revisar, nunca
    `general`. `receq` va DESPUÉS de lo intracomunitario (una adquisición intracomunitaria con recargo sigue siendo
    intracomunitaria para el 349)."""
    pais = a["contraparte"]["pais"]
    if a["contraparte"]["nif"] and pais != "ES":
        c = v1.clave_operacion(doc, mi_nif, forzada)
        if c["clave"] in _INTRA:
            return "intra", None
        if c["clave"] in _IMPEXP:
            return "impexp", None
        return "", ("HOLDED_OPERACION_SIN_CLASIFICAR", f"proveedor no español ({pais or 'país desconocido'}) y no se "
                                                       f"sabe si es intra, impexp u otra cosa: {c['motivo']}")
    fac = doc.get("factura") or {}
    if fac.get("tiene_recargo_equivalencia") or (a["recargo"] or Decimal("0")) > 0:
        return "receq", None
    return "general", None


def clave_iva(tipo, claves):
    """(clave de Holded | '', motivo | None). Con tu fichero de claves, la tuya; sin él, solo la documentada (21)."""
    if tipo is None:
        return "", ("HOLDED_CLAVE_IVA_SIN_TABLA", "una línea sin tipo de IVA")
    if claves:
        if tipo in claves:
            return claves[tipo], None
        return "", ("HOLDED_CLAVE_IVA_SIN_TABLA", f"el tipo {tipo} no está en tu fichero de claves de impuesto")
    if tipo in CLAVE_DOCUMENTADA:
        return CLAVE_DOCUMENTADA[tipo], None
    return "", ("HOLDED_CLAVE_IVA_SIN_TABLA", f"tipo {'de suplido / no sujeto' if tipo == NO_SUJETO else tipo + ' %'}: "
                                              "la ayuda de Holded solo documenta p_iva_21; da tu fichero --claves-iva")


def motivos_holded(a, doc, lineas, op_motivo):
    """Lo que, además de los motivos de la pieza común, impide importar la compra en Holded a ciegas."""
    motivos = [op_motivo] if op_motivo else []
    if a["retencion"]:
        pct = a["porcentaje_retencion"]
        if pct is None:
            motivos.append(("HOLDED_VARIAS_RETENCIONES", "más de un tipo de retención: la columna `Retención %` lleva uno"))
        else:
            primera = lineas[0][4] if lineas and lineas[0][4] is not None else None
            sobre_base = v1.redondear((a["base"] or Decimal("0")) * pct / 100)
            sobre_primera = v1.redondear(primera * pct / 100) if primera is not None else None
            if abs(sobre_base - a["retencion"]) >= v1.TOLERANCIA and (
                    sobre_primera is None or abs(sobre_primera - a["retencion"]) >= v1.TOLERANCIA):
                motivos.append(("HOLDED_RETENCION_NO_CUADRA", f"retención de {a['retencion']} que no es el {v1.texto_tipo(pct)} % "
                                                              "ni de la base ni de la primera línea"))
    if a["es_rectificativa"]:
        motivos.append(("HOLDED_RECTIFICATIVA", "rectificativa: la plantilla de compras no tiene columna para la factura "
                                                f"rectificada (rectifica «{a['rectifica_numero']}»)"))
    if not a["contraparte"]["nombre"]:
        motivos.append(("HOLDED_CONTACTO_SIN_NOMBRE", "`Nombre del contacto` es obligatoria y la respuesta no lo trae"))
    if not lineas:
        motivos.append(("HOLDED_SIN_LINEAS", "ninguna línea ni base que escribir"))
    for concepto, _, precio, tipo, importe in lineas:
        if importe is None:
            motivos.append(("HOLDED_LINEA_SIN_IMPORTE", f"la línea «{concepto}» no trae importe"))
        elif importe == 0:
            motivos.append(("HOLDED_LINEA_A_CERO", f"la línea «{concepto}» va a 0: Holded la importaría sin importe"))
        if tipo == "0" and a["contraparte"]["pais"] == "ES":
            motivos.append(("HOLDED_EXENTO_O_CERO", f"la línea «{concepto}» va al 0 %: si es exenta, `Operación` es "
                                                    "`exento`, y v1 no lo dice"))
    vistos, unicos = set(), []
    for m in motivos:
        if m[0] not in vistos:
            vistos.add(m[0])
            unicos.append(m)
    return unicos


def convertir(entradas, mi_nif="", direccion="", claves=None, cuenta_gasto=""):
    """Todo el lote. Devuelve {"ficheros": {nombre: [filas]}, "revisar": [...], "omitidos": [...]}."""
    claves = claves or {}
    ficheros = {"holded_compras.csv": [], "holded_revisar.csv": []}
    revisar, omitidos = [], []
    dup = v1.duplicadas(entradas, mi_nif, direccion)       # la misma factura dos veces en el lote: la 2.ª a revisar
    for e, duplicada_de in zip(entradas, dup):
        if e["documento"] is None:
            omitidos.append([e["origen"], e["trabajo"], e["omitido"]])
            continue
        doc = e["documento"]
        a = v1.analizar(doc, mi_nif, direccion, duplicada_de)
        fila_rev = lambda nivel, fichero, ms: [e["origen"], e["trabajo"], nivel, a["direccion"], a["numero"],  # noqa: E731
                                               a["contraparte"]["nif"], fichero, " | ".join(c for c, _ in ms)]
        if a["direccion"] != v1.RECIBIDA:
            ms = a["motivos"] + ([("HOLDED_VENTA_SIN_PLANTILLA", "una venta: este conector escribe la plantilla de "
                                   "COMPRAS; la de ventas no está verificada")] if a["direccion"] == v1.EMITIDA else [])
            revisar.append(fila_rev(REVISAR, "", ms))
            continue
        lineas = lineas_holded(doc, a)
        operacion, op_motivo = operacion_factura(doc, a, mi_nif, direccion)
        motivos = a["motivos"] + motivos_holded(a, doc, lineas, op_motivo)
        claves_linea = []
        for *_, tipo, _imp in lineas:
            clave, m = clave_iva(tipo, claves)
            claves_linea.append(clave)
            if m and m[0] not in {c for c, _ in motivos}:
                motivos.append(m)
        apartada = bool(motivos)
        nombre = "holded_revisar.csv" if apartada else "holded_compras.csv"
        marca = "[REV] " if apartada else ""
        pct = a["porcentaje_retencion"]
        retencion = v1.texto_tipo(pct) if (a["retencion"] and pct is not None) else ""
        for i, ((concepto, unidades, precio, tipo, _), clave) in enumerate(zip(lineas, claves_linea)):
            ficheros[nombre].append([
                a["numero"], _fecha(a["fecha"]), _fecha(a["fecha"]),        # v1 no trae vencimiento: el de emisión
                a["contraparte"]["nombre"], a["contraparte"]["nif"], marca + concepto, precio, unidades, clave,
                "0",                                                         # cobrada: 0 en todas (factura sin pagar)
                retencion if i == 0 else "",                                 # la retención es de la factura: 1.ª línea
                "",                                                          # ISP: v1 no lo trae; vacía, no 0
                "nosujeto" if tipo == NO_SUJETO else operacion,
                cuenta_gasto])
        if apartada:
            revisar.append(fila_rev(REVISAR, nombre, motivos))
        elif a["notas"]:             # AJUSTE_REDONDEO: 1-2 céntimos fundidos en la línea mayor; se dice, no se aparta
            revisar.append(fila_rev(AVISO, nombre, a["notas"]))
    return {"ficheros": {n: f for n, f in ficheros.items() if f}, "revisar": revisar, "omitidos": omitidos}


# ─── salida ───────────────────────────────────────────────────────────────────

def a_excel_es(celda):
    """Solo la celda que es EXACTAMENTE un número con decimales cambia el punto por la coma (como el portal)."""
    return celda.replace(".", ",") if _SOLO_DECIMAL.match(celda) else celda


def escribir_holded(ruta, filas, excel_es=False):
    """Cabecera sin comillas; datos entre comillas; ';'; '\\n'. UTF-8 sin marca, o con ella y coma en la copia Excel."""
    with open(ruta, "w", encoding="utf-8-sig" if excel_es else "utf-8", newline="") as fh:
        fh.write(";".join(CABECERA) + "\n")
        w = csv.writer(fh, delimiter=";", quoting=csv.QUOTE_ALL, lineterminator="\n")
        for f in filas:
            celdas = [v1.celda_segura(c) for c in f]
            w.writerow([a_excel_es(c) for c in celdas] if excel_es else celdas)


def escribir_informe(ruta, cabecera, filas):
    """revisar.csv y omitidos.csv: el formato común de los conectores (punto y coma, todo entre comillas)."""
    with open(ruta, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter=";", quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        w.writerow(cabecera)
        for f in filas:
            w.writerow([v1.celda_segura(c) for c in f])


def main(argv=None):
    p = argparse.ArgumentParser(description="Respuesta v1 de MuninCloud -> CSV de la plantilla de compras de Holded")
    p.add_argument("entradas", nargs="+", help="ficheros .json o directorios con las respuestas de la API")
    p.add_argument("--salida", required=True, help="directorio donde escribir los ficheros")
    p.add_argument("--mi-nif", default="", help="el NIF de tu empresa: comprueba la dirección que da la API y la decide "
                                                "si la respuesta no la trae")
    p.add_argument("--direccion", choices=["RECIBIDA", "EMITIDA"], default="",
                   help="fuerza la dirección de TODAS las facturas (manda sobre la que da la API)")
    p.add_argument("--claves-iva", default="", help="fichero «tipo;clave» con las claves de impuesto de TU cuenta de "
                                                    "Holded (sin él, solo p_iva_21 y el resto a revisar)")
    p.add_argument("--cuenta-gasto", default="", help="la cuenta de gasto (629 -> 62900000); sin ella, la columna va vacía")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION} (lectura v1 {v1.VERSION_V1})")
    args = p.parse_args(argv)

    try:
        claves = leer_claves(args.claves_iva)
    except (OSError, ValueError) as e:
        print(f"error de configuración: {e}", file=sys.stderr)
        return 2
    cuenta = v1.ajustar_longitud(args.cuenta_gasto, LONGITUD_CUENTA)
    if args.cuenta_gasto and not cuenta:
        print(f"error de configuración: la cuenta de gasto «{args.cuenta_gasto}» no tiene ninguna cifra", file=sys.stderr)
        return 2

    r = convertir(v1.leer_entradas(args.entradas), args.mi_nif, args.direccion, claves, cuenta)
    os.makedirs(args.salida, exist_ok=True)
    escritos = []
    if r["ficheros"].get("holded_compras.csv"):
        escribir_holded(os.path.join(args.salida, "holded_compras.csv"), r["ficheros"]["holded_compras.csv"])
        escribir_holded(os.path.join(args.salida, "holded_compras_excel_es.csv"), r["ficheros"]["holded_compras.csv"], True)
        escritos += ["holded_compras.csv", "holded_compras_excel_es.csv"]
    if r["ficheros"].get("holded_revisar.csv"):
        escribir_holded(os.path.join(args.salida, "holded_revisar.csv"), r["ficheros"]["holded_revisar.csv"])
        escritos.append("holded_revisar.csv")
    escribir_informe(os.path.join(args.salida, "revisar.csv"),
                     ["documento", "trabajo", "nivel", "direccion", "numero", "nif_contraparte", "fichero", "motivos"],
                     r["revisar"])
    escribir_informe(os.path.join(args.salida, "omitidos.csv"), ["documento", "trabajo", "motivo"], r["omitidos"])
    print(f"ficheros={','.join(escritos) or 'ninguno'} apartadas={len(r['revisar'])} "
          f"omitidos={len(r['omitidos'])} salida={args.salida}")
    for f in r["revisar"]:
        print(f"  apartada {f[0]}: {f[7]}")
    for f in r["omitidos"]:
        print(f"  omitido  {f[0]}: {f[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
