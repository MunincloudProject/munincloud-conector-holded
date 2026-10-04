#!/usr/bin/env python3
"""Vía B del conector de Holded: OTRO lector de la salida, que no comparte código con el conector ni con la pieza común.

    python3 pruebas/via_b.py <directorio de salida> <entradas: ficheros .json o directorios> [--claves tipo;clave.csv]

Lee los CSV con su propio analizador (no el módulo csv) y las respuestas JSON por su cuenta, y comprueba:
  - formato: holded_compras.csv en UTF-8 sin marca, '\\n', la cabecera de 14 columnas sin comillas y los datos entre
    comillas; la copia Excel ES con marca y coma decimal, y por lo demás la MISMA (celda a celda);
  - las 10 obligatorias sin vacío en ninguna fila de lo importable; `Cantidad cobrada` = 0 e ISP vacía en todas;
  - `Operación` dentro de los seis valores de Holded (o vacía, solo en lo de revisar);
  - la retención solo en la 1.ª fila de cada factura;
  - ninguna emitida (venta) en ningún fichero de Holded: el proveedor de la respuesta no es mi NIF;
  - por factura importable, la suma de Precio x Unidades (sin las filas `nosujeto`) = la base de la respuesta;
  - ninguna clave de IVA distinta de p_iva_21 sin el fichero de claves (con él, solo claves de ese fichero);
  - toda factura terminada de la entrada sale UNA vez: en holded_compras.csv o en revisar.csv.
Última línea: VIA_B=OK … (rc 0), VIA_B=FALLO:<n> (rc 1) o VIA_B=NO_MEDIBLE (rc 2). Solo biblioteca estándar.
"""
import json
import os
import re
import sys
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CABECERA = ("Núm. factura;Fecha dd/mm/yyyy;Fecha de vencimiento dd/mm/yyyy;Nombre del contacto;NIF;Concepto;"
            "Precio unidad;Unidades;IVA %;Cantidad cobrada;Retención %;Inv. Suj. Pasivo (1/0);Operación;"
            "Núm. cuenta de gasto")
COL = {n: i for i, n in enumerate(CABECERA.split(";"))}
SEIS = {"intra", "general", "impexp", "nosujeto", "exento", "receq"}
MI_NIF = "B00000018"
BOM = "﻿"
DOS = Decimal("0.01")


def celdas(linea):
    """Una línea con TODAS las celdas entre comillas y ';' -> lista. None si alguna celda no va entre comillas."""
    out, i, n = [], 0, len(linea)
    while i < n:
        if linea[i] != '"':
            return None
        i, valor = i + 1, []
        while True:
            j = linea.find('"', i)
            if j < 0:
                return None
            valor.append(linea[i:j])
            if j + 1 < n and linea[j + 1] == '"':
                valor.append('"')
                i = j + 2
                continue
            i = j + 1
            break
        out.append("".join(valor))
        if i < n:
            if linea[i] != ";":
                return None
            i += 1
            if i == n:
                out.append("")
    return out


def leer_holded(ruta, fallos, con_marca=False):
    """[filas] de un fichero de Holded, comprobando el formato. None si no existe."""
    if not os.path.exists(ruta):
        return None
    crudo = open(ruta, "rb").read()
    texto = crudo.decode("utf-8")
    nombre = os.path.basename(ruta)
    if texto.startswith(BOM) != con_marca:
        fallos.append(f"{nombre}: marca UTF-8 {'ausente' if con_marca else 'presente'}")
    texto = texto[1:] if texto.startswith(BOM) else texto
    if "\r" in texto:
        fallos.append(f"{nombre}: lleva '\\r' (se esperaba '\\n')")
    lineas = texto.split("\n")
    if lineas and lineas[-1] == "":
        lineas.pop()
    if not lineas or lineas[0] != CABECERA:
        fallos.append(f"{nombre}: la cabecera no es la de la plantilla de compras")
        return []
    filas = []
    for n, linea in enumerate(lineas[1:], 2):
        if not linea.strip():
            fallos.append(f"{nombre}:{n}: fila en blanco (Holded no importa la plantilla)")
            continue
        c = celdas(linea)
        if c is None or len(c) != len(COL):
            fallos.append(f"{nombre}:{n}: no son {len(COL)} celdas entre comillas")
            continue
        filas.append(c)
    return filas


def leer_json(rutas):
    """{nombre de fichero: documento | None (no terminado)}."""
    docs = {}
    for r in rutas:
        ficheros = ([os.path.join(r, f) for f in sorted(os.listdir(r)) if f.endswith(".json")]
                    if os.path.isdir(r) else [r])
        for f in ficheros:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            terminado = d.get("estado") == "terminado" and isinstance(d.get("documento"), dict)
            docs[os.path.basename(f)] = d["documento"] if terminado else None
    return docs


def num(t):
    try:
        return Decimal(t)
    except (InvalidOperation, ValueError):
        return None


def nif(t):
    return re.sub(r"[\s.\-]", "", str(t or "")).upper()


def fecha_es(iso):
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(iso or ""))
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else None


def leer_claves(ruta):
    claves = set()
    for linea in open(ruta, encoding="utf-8-sig"):
        partes = [p.strip() for p in linea.strip().split(";")]
        if len(partes) == 2 and not linea.startswith("#") and partes[0].lower() != "tipo":
            claves.add(partes[1])
    return claves


def informe_revisar(ruta):
    """{documento: [(nivel, motivos)]} de revisar.csv (formato común: ';' y todo entre comillas, '\\r\\n')."""
    out = {}
    if not os.path.exists(ruta):
        return out
    lineas = open(ruta, encoding="utf-8", newline="").read().split("\r\n")
    cab = celdas(lineas[0]) or []
    for linea in lineas[1:]:
        c = celdas(linea) if linea else None
        if c and len(c) == len(cab):
            out.setdefault(c[cab.index("documento")], []).append((c[cab.index("nivel")], c[cab.index("motivos")]))
    return out


def clave_dup(d):
    """Regla A, sin la pieza: (dirección, NIF de la otra parte y número solo A-Z0-9, fecha); None si falta algo."""
    fac, dirn = d.get("factura") or {}, d.get("direccion")
    otra = d.get("proveedor") if dirn == "RECIBIDA" else d.get("cliente") if dirn == "EMITIDA" else None
    n = re.sub(r"[^A-Z0-9]", "", str((otra or {}).get("cif_nif") or "").upper())
    numero = re.sub(r"[^A-Z0-9]", "", str(fac.get("numero") or "").upper())
    f = str(fac.get("fecha") or "")
    return (dirn, n, numero, f) if otra is not None and n and numero and re.match(r"^\d{4}-\d{2}-\d{2}$", f) else None


def es_redondeo(d):
    """Regla B, sin la pieza: base y cuota de las líneas sujetas a 2 céntimos o menos de la cabecera, y alguna distinta."""
    fac = d.get("factura") or {}
    lineas = [x or {} for x in d.get("lineas") or [] if not (x or {}).get("es_no_sujeto")]
    if not lineas:
        return False
    base = sum((num(str(x.get("amount"))) or Decimal("0")) for x in lineas)
    cuota = sum(((num(str(x.get("amount"))) or Decimal("0")) * (num(str(x.get("tax_percent"))) or Decimal("0")) / 100)
                .quantize(DOS, rounding=ROUND_HALF_UP) for x in lineas)
    db = (num(str(fac.get("base_total"))) or Decimal("0")) - base
    dc = (num(str(fac.get("iva_total"))) or Decimal("0")) - cuota
    return (db != 0 or dc != 0) and abs(db) <= 2 * DOS and abs(dc) <= 2 * DOS


def facturas(filas):
    """Agrupa filas consecutivas por (número, NIF): [(clave, [filas])]."""
    grupos = []
    for f in filas:
        k = (f[COL["Núm. factura"]], f[COL["NIF"]])
        if grupos and grupos[-1][0] == k:
            grupos[-1][1].append(f)
        else:
            grupos.append((k, [f]))
    return grupos


def comprobar(salida, entradas, claves=None):
    fallos = []
    docs = leer_json(entradas)
    importable = leer_holded(os.path.join(salida, "holded_compras.csv"), fallos) or []
    excel = leer_holded(os.path.join(salida, "holded_compras_excel_es.csv"), fallos, con_marca=True)
    rev = leer_holded(os.path.join(salida, "holded_revisar.csv"), fallos) or []
    if importable and excel is None:
        fallos.append("falta la copia holded_compras_excel_es.csv")
    elif excel is not None:
        if len(excel) != len(importable):
            fallos.append("la copia Excel ES no tiene las mismas filas")
        for a, b in zip(importable, excel):
            for x, y in zip(a, b):
                esperado = x.replace(".", ",") if re.match(r"^-?\d+\.\d+$", x) else x
                if y != esperado:
                    fallos.append(f"copia Excel ES: «{y}» donde se esperaba «{esperado}»")
                    break
    por_numero, primeras, duplicada_de = {}, {}, {}
    for nombre, d in docs.items():          # en el orden del lote (los nombres, ordenados)
        k = clave_dup(d) if d else None
        if k is not None and k in primeras:
            duplicada_de[nombre] = primeras[k]
            continue                          # la 2.ª y siguientes no pueden estar en lo importable
        if k is not None:
            primeras[k] = nombre
        if d:
            fac = d.get("factura") or {}
            por_numero.setdefault((str(fac.get("numero") or "").strip(), nif((d.get("proveedor") or {}).get("cif_nif"))),
                                  []).append(nombre)
    vistos = []
    for fichero, filas in (("holded_compras.csv", importable), ("holded_revisar.csv", rev)):
        for (numero, nif_fila), grupo in facturas(filas):
            nombres = por_numero.get((numero.strip(), nif(nif_fila)), [])
            etiqueta = f"{fichero} {numero}"
            for i, f in enumerate(grupo):
                op = f[COL["Operación"]]
                if op not in SEIS and not (op == "" and fichero == "holded_revisar.csv"):
                    fallos.append(f"{etiqueta}: Operación «{op}» fuera de los seis")
                if i > 0 and f[COL["Retención %"]]:
                    fallos.append(f"{etiqueta}: retención fuera de la 1.ª fila")
                if f[COL["Cantidad cobrada"]] != "0" or f[COL["Inv. Suj. Pasivo (1/0)"]] != "":
                    fallos.append(f"{etiqueta}: cobrada distinta de 0 o ISP no vacía")
                if fichero == "holded_compras.csv":
                    vacias = [n for n in list(COL)[:10] if not f[COL[n]].strip()]
                    if vacias:
                        fallos.append(f"{etiqueta}: obligatorias vacías {vacias}")
                    clave = f[COL["IVA %"]]
                    if (claves is None and clave != "p_iva_21") or (claves is not None and clave not in claves):
                        fallos.append(f"{etiqueta}: clave de IVA «{clave}» sin tabla que la respalde")
            if fichero != "holded_compras.csv":
                continue
            if nif(nif_fila) == MI_NIF:
                fallos.append(f"{etiqueta}: el proveedor soy yo: es una venta")
            if len(nombres) != 1:
                fallos.append(f"{etiqueta}: {len(nombres)} respuestas casan con número y NIF (se esperaba 1)")
                continue
            d = docs[nombres[0]]
            fac = d.get("factura") or {}
            vistos.append(nombres[0])
            if nif((d.get("cliente") or {}).get("cif_nif")) != MI_NIF or str(d.get("direccion") or "RECIBIDA") != "RECIBIDA":
                fallos.append(f"{etiqueta}: la respuesta no es una compra mía")
            if grupo[0][COL["Fecha dd/mm/yyyy"]] != fecha_es(fac.get("fecha")):
                fallos.append(f"{etiqueta}: fecha distinta de la respuesta")
            suma = Decimal("0")
            for f in grupo:
                p, u = num(f[COL["Precio unidad"]]), num(f[COL["Unidades"]])
                if p is None or u is None:
                    fallos.append(f"{etiqueta}: precio o unidades no numéricos")
                    break
                if f[COL["Operación"]] != "nosujeto":
                    suma += p * u
            base = num(str(fac.get("base_total")))
            if base is None or abs(suma.quantize(DOS) - base.quantize(DOS)) >= DOS:
                fallos.append(f"{etiqueta}: Precio x Unidades = {suma} y la base de la respuesta es {base}")
    informe = informe_revisar(os.path.join(salida, "revisar.csv"))
    apartadas = {n for n, filas in informe.items() if any(nv == "REVISAR" for nv, _ in filas)}
    for nombre, d in docs.items():
        if d is None:
            continue
        motivos = " | ".join(m for _, m in informe.get(nombre, []))
        if nombre in duplicada_de and (nombre not in apartadas or "POSIBLE_DUPLICADA" not in motivos):
            fallos.append(f"{nombre}: es la misma factura que {duplicada_de[nombre]} y no va a revisar con "
                          "POSIBLE_DUPLICADA")
        if nombre not in duplicada_de and es_redondeo(d):
            if nombre in apartadas and "NO_CUADRA" in motivos:
                fallos.append(f"{nombre}: apartada por un redondeo de 1-2 céntimos (regla B: se importa)")
            if nombre in vistos and "AJUSTE_REDONDEO" not in motivos:
                fallos.append(f"{nombre}: importada con un redondeo de 1-2 céntimos y sin el aviso AJUSTE_REDONDEO")
        veces = vistos.count(nombre) + (1 if nombre in apartadas else 0)
        if veces != 1:
            fallos.append(f"{nombre}: sale {veces} veces (en lo importable o en revisar.csv); se esperaba 1")
    return fallos, len(importable), len(docs)


def main(argv):
    global MI_NIF
    claves = None
    if "--mi-nif" in argv:                      # el NIF de TU empresa (por omisión, el de prueba de los ejemplos)
        i = argv.index("--mi-nif")
        MI_NIF, argv = nif(argv[i + 1]), argv[:i] + argv[i + 2:]
    if "--claves" in argv:
        i = argv.index("--claves")
        claves, argv = leer_claves(argv[i + 1]), argv[:i] + argv[i + 2:]
    if len(argv) < 2 or not os.path.isdir(argv[0]):
        print("uso: via_b.py <salida> <entradas…> [--claves f] [--mi-nif NIF]\nVIA_B=NO_MEDIBLE")
        return 2
    try:
        fallos, n_filas, n_docs = comprobar(argv[0], argv[1:], claves)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        print(f"VIA_B=NO_MEDIBLE {e}")
        return 2
    for f in fallos:
        print("  " + f)
    if fallos:
        print(f"VIA_B=FALLO:{len(fallos)}")
        return 1
    print(f"VIA_B=OK filas_importables={n_filas} respuestas={n_docs}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
