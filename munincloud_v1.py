"""Lectura de la respuesta `v1` de la API de auditoría de facturas de MuninCloud.

Pieza común de los conectores públicos (`csv_generico`, `a3`). Va COPIADA, idéntica, en cada repositorio para
que cada uno funcione solo; si la cambias en uno, cámbiala en todos (su huella la comparan los bancos de fuera del
repositorio, no las pruebas de dentro).

Qué hace y qué no:
  - Lee lo que devuelve la API: el sobre `Trabajo` (GET /trabajos/{id}), el `documento` suelto o una lista de ellos.
  - La DIRECCIÓN (RECIBIDA o EMITIDA) la LEE de `documento.direccion`. Desde v1.3 es la que DECLARA quien sube la
    factura (`tipo`: compra → RECIBIDA, venta → EMITIDA); si en el papel la empresa de la llave sale en el lado
    contrario, la respuesta trae el aviso TIPO_DISCREPANTE y la factura sale para revisar por él (y solo por él).
    Si das tu NIF y la respuesta no trae ese aviso (una v1.1 o v1.2), además la COMPRUEBA contra el papel: si tu
    NIF está solo en el otro lado, sale para revisar (DIRECCION_CONTRADICE_NIF), pero manda lo que dice la API.
    Solo si la respuesta no trae `direccion` (una anterior a v1.1) la decide como antes: tu NIF contra el emisor y
    el receptor (`proveedor` es SIEMPRE quien emite y `cliente` quien recibe; `lectura_papel` trae `emisor` y
    `receptor` leídos del papel). Si tampoco así sale de un solo lado, es DESCONOCIDA y la factura sale marcada
    para revisar: no se adivina.
  - Agrupa las líneas por tipo de IVA (un registro por factura y tipo), con los suplidos y lo no sujeto aparte.
    Si lo que dan las líneas difiere de la cabecera en 1 o 2 céntimos (en la base, en la cuota o en las dos), es
    redondeo: la diferencia se FUNDE en la línea y el registro de mayor base (`ajuste_redondeo`, `lineas`), para que
    registros y líneas cuadren exacto con la cabecera, y la factura no sale a revisar por eso (queda la nota
    AJUSTE_REDONDEO). Más de 2 céntimos sigue siendo un registro AJUSTE y la factura sale a revisar.
  - Marca la misma factura repetida en un lote (`duplicadas`: dirección, NIF de la contraparte, número y fecha): la
    primera sigue su camino y la segunda y siguientes salen a revisar (POSIBLE_DUPLICADA). Nunca se funden.
  - Comprueba el cuadre: base + IVA + suplidos + recargo - retención = total. El recargo de equivalencia es
    `factura.recargo_total` si la respuesta lo trae (v1.1; lo da el motor de lectura, no se calcula); si no lo trae
    y la factura declara `tiene_recargo_equivalencia`, se calcula por el art. 161 LIVA sobre cada base; si no, cero.
    El recargo que viene se reparte por tipo de IVA solo si hay un único tipo y su porcentaje legal da ese importe;
    si no, la factura sale para revisar (RECARGO_SIN_DESGLOSE): a3 lo necesita por línea y no se supone.
    Si la respuesta ya trae el aviso DESCUADRE, la factura sale por él (AVISO_DESCUADRE) y no además por el propio
    (DESCUADRE), que queda para las respuestas que no lo traen; `descuadre` y `cuadra` se calculan igual.
  - Marca para revisar lo que ningún programa de destino puede recibir tal cual: un NIF que la AEAT da de BAJA o
    REVOCADO (NIF_AEAT_BAJA_O_REVOCADO) o NO IDENTIFICADO (NIF_AEAT_NO_IDENTIFICADO), y una divisa que no es EUR
    (DIVISA_NO_EUR).
  - NO inventa: la cuenta contable, el régimen de IVA y la clave de operación no vienen en `v1` y `analizar` no
    los deduce. Lo que no viene queda vacío, y lo que no cuadra sale con su motivo. Para el conector que los
    necesite hay dos AYUDAS OPCIONALES, que `analizar` no llama: `clave_operacion()` (proveedor extranjero sin IVA
    español: el país decide UE o no, y el concepto de la línea mayor, bien o servicio) y `leer_maestro()` (el
    fichero «NIF;cuenta» o «NIF;id» que exporta el programa del cliente), con `ajustar_longitud()`.

Solo biblioteca estándar de Python (3.8 o posterior).
"""
import json
import os
import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

VERSION_V1 = "1.3.0"

TOLERANCIA = Decimal("0.02")          # por debajo es redondeo del propio papel, no un descuadre
AJUSTE_MAXIMO = Decimal("0.02")       # 1 o 2 céntimos entre líneas y cabecera: redondeo, se funde en el registro mayor
CENTIMO = Decimal("0.01")
TIPOS_IVA_LEGALES = tuple(Decimal(t) for t in ("0", "2", "4", "5", "7", "7.5", "9.5", "10", "15", "21"))
RECARGO_EQUIVALENCIA = {Decimal("21"): Decimal("5.2"), Decimal("10"): Decimal("1.4"), Decimal("4"): Decimal("0.5")}
PAISES_UE = frozenset("AT BE BG CY CZ DE DK EE EL ES FI FR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK XI".split())

RECIBIDA, EMITIDA, DESCONOCIDA = "RECIBIDA", "EMITIDA", "DESCONOCIDA"

# v1.3: estados del censo de la AEAT (`verificacion_identidad.situacion`) con los que la factura no se contabiliza
# sin mirarla. Es el mismo criterio que la hoja de revisión del portal.
# El NIF ya no es operativo para la AEAT (como ESTADOS_NIF_GRAVES del portal): cada situación con su motivo.
MOTIVO_POR_SITUACION_NIF = {"IDENTIFICADO-BAJA": "NIF_AEAT_BAJA_O_REVOCADO",
                            "IDENTIFICADO-REVOCADO": "NIF_AEAT_BAJA_O_REVOCADO",
                            "NO IDENTIFICADO": "NIF_AEAT_NO_IDENTIFICADO"}
SITUACIONES_NIF_GRAVES = frozenset(MOTIVO_POR_SITUACION_NIF)
DIVISA_DE_LOS_PROGRAMAS = "EUR"      # ningún programa de destino recibe hoy la divisa: lo demás se mira a mano

# Lo que la API de MuninCloud deja en un campo cuando la lectura NO pudo leer el dato: no es un valor y no viaja al
# programa contable.
_CENTINELA = re.compile(r"^(S/N|NO_VISIBLE|NO[ _].*VISIBLE)$", re.IGNORECASE)
_NIF_ES = re.compile(r"^[A-Z0-9][0-9]{7}[A-Z0-9]$")
_FECHA = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


# ─── valores ──────────────────────────────────────────────────────────────────

def decimal(valor):
    """Un número de la respuesta como Decimal, o None si no viene o no es un número."""
    if valor is None or isinstance(valor, bool) or valor == "":
        return None
    try:
        return Decimal(str(valor))
    except (InvalidOperation, ValueError):
        return None


def redondear(valor):
    """Dos decimales, redondeo comercial (0,005 sube), y nunca -0.00."""
    r = valor.quantize(CENTIMO, rounding=ROUND_HALF_UP)
    return r if r != 0 else Decimal("0.00")


def leido(valor):
    """El texto tal como se leyó, o '' si no viene o es un centinela de «no visible»."""
    if valor is None:
        return ""
    t = str(valor).strip()
    return "" if _CENTINELA.match(t) else t


def normalizar_nif(valor):
    """Mayúsculas, sin espacios, puntos ni guiones; sin el prefijo ES de un NIF español."""
    t = re.sub(r"[^A-Z0-9]", "", leido(valor).upper())
    if len(t) == 11 and t.startswith("ES") and _NIF_ES.match(t[2:]):
        t = t[2:]
    return t


def pais_del_nif(nif):
    """'ES' para un NIF español, el prefijo para un VAT europeo, '' si no se sabe. Es un dato, no un juicio."""
    n = normalizar_nif(nif)
    if len(n) >= 2 and n[:2].isalpha() and n[:2] in PAISES_UE and not _NIF_ES.match(n):
        return n[:2]
    if _NIF_ES.match(n):
        return "ES"
    return ""


def fecha_valida(valor):
    """'AAAA-MM-DD' si la fecha existe en el calendario; '' si no."""
    m = _FECHA.match(leido(valor))
    if not m:
        return ""
    anio, mes, dia = (int(x) for x in m.groups())
    if not 1 <= mes <= 12:
        return ""
    dias = [31, 29 if (anio % 4 == 0 and (anio % 100 != 0 or anio % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mes - 1]
    return m.group(0) if 1 <= dia <= dias else ""


def texto_tipo(tipo):
    """21 -> '21', 5.2 -> '5.2', 7.50 -> '7.5'."""
    if tipo is None:
        return ""
    t = format(tipo.normalize(), "f")
    return t.rstrip("0").rstrip(".") if "." in t else t


# ─── entrada ──────────────────────────────────────────────────────────────────

def _clasificar(obj, origen, trabajo=""):
    """Un objeto JSON -> una entrada {origen, trabajo, documento|None, omitido}."""
    if isinstance(obj, dict) and "estado" in obj and ("trabajo" in obj or "documento" in obj or "error" in obj):
        trabajo = str(obj.get("trabajo") or trabajo)
        if obj.get("estado") == "terminado" and isinstance(obj.get("documento"), dict):
            return _clasificar(obj["documento"], origen, trabajo)
        if obj.get("estado") == "rechazado":
            codigo = (obj.get("error") or {}).get("codigo") or "SIN_CODIGO"
            return {"origen": origen, "trabajo": trabajo, "documento": None, "omitido": f"rechazado por la API: {codigo}"}
        return {"origen": origen, "trabajo": trabajo, "documento": None,
                "omitido": f"el trabajo no ha terminado (estado {obj.get('estado')})"}
    if isinstance(obj, dict) and isinstance(obj.get("factura"), dict) and isinstance(obj.get("lineas"), list):
        return {"origen": origen, "trabajo": trabajo, "documento": obj, "omitido": ""}
    if isinstance(obj, dict) and "doc_type" in obj:
        return {"origen": origen, "trabajo": trabajo, "documento": None,
                "omitido": f"es una clasificación, no una factura (doc_type {obj.get('doc_type')})"}
    return {"origen": origen, "trabajo": trabajo, "documento": None, "omitido": "no es una respuesta v1 reconocible"}


def leer_entradas(rutas):
    """Ficheros .json o directorios (se leen sus .json en orden). Devuelve la lista de entradas."""
    ficheros = []
    for ruta in rutas:
        if os.path.isdir(ruta):
            ficheros += [os.path.join(ruta, f) for f in sorted(os.listdir(ruta)) if f.lower().endswith(".json")]
        else:
            ficheros.append(ruta)
    entradas = []
    for fichero in ficheros:
        nombre = os.path.basename(fichero)
        try:
            with open(fichero, encoding="utf-8") as fh:
                obj = json.load(fh)
        except (OSError, ValueError) as e:
            entradas.append({"origen": nombre, "trabajo": "", "documento": None,
                             "omitido": f"no se pudo leer: {type(e).__name__}: {e}"})
            continue
        if isinstance(obj, list):
            entradas += [_clasificar(o, f"{nombre}#{i + 1}") for i, o in enumerate(obj)]
        else:
            entradas.append(_clasificar(obj, nombre))
    return entradas


# ─── la factura ───────────────────────────────────────────────────────────────

def direccion(doc, mi_nif="", forzada=""):
    """RECIBIDA, EMITIDA o DESCONOCIDA. Manda `forzada` si se da; si no, `documento.direccion` (v1.1); y solo si la
    respuesta no la trae, la que sale de tu NIF contra el papel (`direccion_por_nif`)."""
    if forzada:
        f = forzada.strip().upper()
        if f not in (RECIBIDA, EMITIDA):
            raise ValueError("la dirección forzada tiene que ser RECIBIDA o EMITIDA")
        return f
    dada = str(doc.get("direccion") or "").strip().upper()
    if dada in (RECIBIDA, EMITIDA):
        return dada
    return direccion_por_nif(doc, mi_nif)


def direccion_por_nif(doc, mi_nif=""):
    """Tu NIF contra el emisor y el receptor: RECIBIDA, EMITIDA o DESCONOCIDA (si no está en un solo lado)."""
    mi = normalizar_nif(mi_nif)
    if not mi:
        return DESCONOCIDA
    papel = doc.get("lectura_papel") or {}
    emisores = {normalizar_nif((doc.get("proveedor") or {}).get("cif_nif")), normalizar_nif(papel.get("emisor"))} - {""}
    receptores = {normalizar_nif((doc.get("cliente") or {}).get("cif_nif")), normalizar_nif(papel.get("receptor"))} - {""}
    soy_emisor, soy_receptor = mi in emisores, mi in receptores
    if soy_emisor and not soy_receptor:
        return EMITIDA
    if soy_receptor and not soy_emisor:
        return RECIBIDA
    return DESCONOCIDA


def partes(doc, dirn):
    """(emisor, receptor, contraparte): cada uno {nombre, nif, pais}. La contraparte depende de la dirección."""
    def parte(t):
        t = t or {}
        nif = normalizar_nif(t.get("cif_nif"))
        return {"nombre": leido(t.get("nombre")), "nif": nif, "pais": pais_del_nif(nif) if nif else ""}
    emisor, receptor = parte(doc.get("proveedor")), parte(doc.get("cliente"))
    contraparte = emisor if dirn == RECIBIDA else receptor if dirn == EMITIDA else {"nombre": "", "nif": "", "pais": ""}
    return emisor, receptor, contraparte


def retencion(doc):
    """(porcentaje|None, cuota|None). Manda el importe que trae la respuesta; sin importe no se calcula."""
    cuota, tipos, sin_importe = Decimal("0"), set(), False
    for imp in doc.get("impuestos_y_retenciones") or []:
        tipo, importe = decimal((imp or {}).get("rate")), decimal((imp or {}).get("importe"))
        if not ((tipo is not None and tipo < 0) or (importe is not None and importe < 0)):
            continue
        if importe is None:
            sin_importe = True
            continue
        cuota += abs(importe)
        if tipo is not None:
            tipos.add(abs(tipo))
    if sin_importe:
        return (None, None)
    return (tipos.pop() if len(tipos) == 1 else None, redondear(cuota))


def tipo_efectivo(base, cuota):
    """El tipo legal que explica cuota/base (a medio punto), o None. Solo para facturas sin líneas."""
    if base is None or cuota is None or base == 0:
        return Decimal("0") if (cuota == 0 and base) else None
    t = cuota / base * 100
    for legal in TIPOS_IVA_LEGALES:
        if abs(t - legal) < Decimal("0.5"):
            return legal
    return None


def _agrupar(lineas):
    """Las líneas por (clase, tipo): base sumada y cuota = suma de base x tipo de cada línea, a céntimos."""
    grupos = {}
    for linea in lineas:
        linea = linea or {}
        importe = decimal(linea.get("amount")) or Decimal("0")
        no_sujeto = bool(linea.get("es_no_sujeto"))
        tipo = Decimal("0") if no_sujeto else (decimal(linea.get("tax_percent")) or Decimal("0"))
        clave = ("NO_SUJETO" if no_sujeto else "IVA", tipo)
        g = grupos.setdefault(clave, {"clase": clave[0], "tipo_iva": tipo, "base": Decimal("0"),
                                      "cuota": Decimal("0"), "origen": "LINEAS"})
        g["base"] += importe
        g["cuota"] += Decimal("0") if no_sujeto else redondear(importe * tipo / 100)
    return [dict(g, base=redondear(g["base"]), cuota=redondear(g["cuota"])) for g in grupos.values()]


def _desvios(registros, base_cab, iva_cab):
    """(base, cuota) de los registros de IVA menos la cabecera, a céntimos."""
    base_iva = sum((r["base"] for r in registros if r["clase"] == "IVA"), Decimal("0"))
    cuota_iva = sum((r["cuota"] for r in registros if r["clase"] == "IVA"), Decimal("0"))
    return redondear(base_iva - (base_cab or Decimal("0"))), redondear(cuota_iva - (iva_cab or Decimal("0")))


def ajuste_redondeo(doc):
    """Si las líneas difieren de la cabecera en 1 o 2 céntimos (base, cuota o las dos), lo que se funde y dónde:
    {linea: índice en `lineas`, tipo_iva, base: lo que se suma a esa línea, cuota: lo que se suma a la cuota de su
    registro tras sumar la base, dif_base, dif_cuota: cabecera menos líneas}. None si cuadra o si la diferencia es
    mayor (entonces sale el registro AJUSTE y la factura a revisar). Es lo que hace el portal (fundir el ajuste en
    el registro principal): el registro y la línea de MAYOR base de IVA."""
    fac = doc.get("factura") or {}
    base_cab, iva_cab = decimal(fac.get("base_total")), decimal(fac.get("iva_total"))
    lineas = [l or {} for l in doc.get("lineas") or []]
    if base_cab is None or iva_cab is None or not lineas:
        return None
    regs = _agrupar(lineas)
    db, dc = _desvios(regs, base_cab, iva_cab)
    if (db == 0 and dc == 0) or abs(db) > AJUSTE_MAXIMO or abs(dc) > AJUSTE_MAXIMO:
        return None
    con_iva = [r for r in regs if r["clase"] == "IVA"]
    if not con_iva:
        return None
    mayor = max(con_iva, key=lambda r: (abs(r["base"]), r["tipo_iva"]))
    candidatas = [i for i, l in enumerate(lineas) if not l.get("es_no_sujeto")
                  and (decimal(l.get("tax_percent")) or Decimal("0")) == mayor["tipo_iva"]]
    i = max(candidatas, key=lambda j: (abs(decimal(lineas[j].get("amount")) or Decimal("0")), -j))
    ajustadas = [dict(l) for l in lineas]
    ajustadas[i]["amount"] = redondear((decimal(lineas[i].get("amount")) or Decimal("0")) - db)
    _, dc2 = _desvios(_agrupar(ajustadas), base_cab, iva_cab)
    return {"linea": i, "tipo_iva": mayor["tipo_iva"], "base": redondear(-db), "cuota": redondear(-dc2),
            "dif_base": redondear(-db), "dif_cuota": redondear(-dc)}


def lineas(doc):
    """Las líneas de la respuesta (copias), con el redondeo de `ajuste_redondeo` ya fundido en el `amount` de la
    línea mayor (que lleva además `ajuste_redondeo`: lo sumado). Es lo que debe escribir un conector que lleve
    líneas: así suman exacto la base de la cabecera. Sin ajuste, iguales a las de la respuesta."""
    copia = [dict(l or {}) for l in doc.get("lineas") or []]
    aj = ajuste_redondeo(doc)
    if aj and aj["base"] != 0:
        l = copia[aj["linea"]]
        l["amount"] = redondear((decimal(l.get("amount")) or Decimal("0")) + aj["base"])
        l["ajuste_redondeo"] = aj["base"]
    return copia


def registros_iva(doc):
    """Una entrada por tipo de IVA (y una para suplidos/no sujeto), con base y cuota.

    clase: IVA | NO_SUJETO | AJUSTE.  origen: LINEAS (sumado de las líneas) | CABECERA (factura sin líneas) |
    AJUSTE (lo que las líneas no explican de la cabecera; manda la cabecera y la diferencia se ve).
    La cuota de cada línea es base x tipo redondeada a céntimos; `v1` no trae la cuota por línea.
    1 o 2 céntimos de diferencia no hacen registro AJUSTE: se funden en el registro de mayor base
    (`ajuste_redondeo`), que queda cuadrando exacto con la cabecera.
    """
    fac = doc.get("factura") or {}
    base_cab, iva_cab = decimal(fac.get("base_total")), decimal(fac.get("iva_total"))
    aj = ajuste_redondeo(doc)
    registros = _agrupar(lineas(doc))
    if aj and aj["cuota"] != 0:
        for r in registros:
            if r["clase"] == "IVA" and r["tipo_iva"] == aj["tipo_iva"]:
                r["cuota"] = redondear(r["cuota"] + aj["cuota"])
                break
    if not registros and ((base_cab or 0) != 0 or (iva_cab or 0) != 0):
        registros = [{"clase": "IVA", "tipo_iva": tipo_efectivo(base_cab, iva_cab),
                      "base": redondear(base_cab or Decimal("0")), "cuota": redondear(iva_cab or Decimal("0")),
                      "origen": "CABECERA"}]
    desvio_base, desvio_cuota = _desvios(registros, base_cab, iva_cab)
    if abs(desvio_base) > AJUSTE_MAXIMO or abs(desvio_cuota) > AJUSTE_MAXIMO:
        registros.append({"clase": "AJUSTE", "tipo_iva": None, "base": redondear(-desvio_base),
                          "cuota": redondear(-desvio_cuota), "origen": "AJUSTE"})
    registros = [r for r in registros if r["base"] != 0 or r["cuota"] != 0]
    orden = {"IVA": 0, "NO_SUJETO": 1, "AJUSTE": 2}
    registros.sort(key=lambda r: (orden[r["clase"]], -(r["tipo_iva"] or 0)))
    tiene_re = bool(fac.get("tiene_recargo_equivalencia"))
    for r in registros:
        pct = RECARGO_EQUIVALENCIA.get(r["tipo_iva"]) if (tiene_re and r["clase"] == "IVA") else None
        r["tipo_recargo"] = pct
        r["cuota_recargo"] = redondear(r["base"] * pct / 100) if pct else None
    # v1.1: un recargo que VIENE (factura.recargo_total) y la factura no declara: se pone en su único tipo de IVA
    # si el porcentaje legal de ese tipo da exactamente ese importe. Si no, los registros no lo llevan y la factura
    # sale para revisar (analizar: RECARGO_SIN_DESGLOSE). No se reparte a ojo entre varios tipos.
    dado = recargo_dado(doc)
    if dado and not tiene_re:
        con_iva = [r for r in registros if r["clase"] == "IVA"]
        if len(con_iva) == 1:
            r = con_iva[0]
            pct = RECARGO_EQUIVALENCIA.get(r["tipo_iva"])
            if pct is not None and abs(redondear(r["base"] * pct / 100) - dado) < TOLERANCIA:
                r["tipo_recargo"], r["cuota_recargo"] = pct, dado
    return registros


def recargo_dado(doc):
    """`factura.recargo_total` como Decimal, o None si la respuesta no lo trae (o viene null)."""
    valor = (doc.get("factura") or {}).get("recargo_total")
    if isinstance(valor, bool):
        return None
    d = decimal(valor)
    return redondear(d) if d is not None else None


def numero_normalizado(valor):
    """El número de factura para comparar: mayúsculas y solo letras y cifras («F-2026/001» = «f 2026 001»)."""
    return re.sub(r"[^A-Z0-9]", "", leido(valor).upper())


def clave_duplicada(doc, mi_nif="", forzada=""):
    """(dirección, NIF de la contraparte, número, fecha) normalizados, o None si falta alguno: sin los cuatro no
    se puede decir que dos respuestas son la misma factura."""
    fac = doc.get("factura") or {}
    dirn = direccion(doc, mi_nif, forzada)
    clave = (dirn, partes(doc, dirn)[2]["nif"], numero_normalizado(fac.get("numero")), fecha_valida(fac.get("fecha")))
    return None if dirn == DESCONOCIDA or not all(clave) else clave


def duplicadas(entradas, mi_nif="", forzada=""):
    """Sobre TODO el lote (la lista de `leer_entradas`): para cada entrada, '' o el `origen` de la primera entrada
    con la misma factura (`clave_duplicada`). La primera sigue su camino; la segunda y siguientes se pasan a
    `analizar(..., duplicada_de=...)` y salen a revisar con POSIBLE_DUPLICADA. Nunca se funden."""
    primera, salida = {}, []
    for e in entradas:
        doc = e.get("documento")
        clave = clave_duplicada(doc, mi_nif, forzada) if isinstance(doc, dict) else None
        if clave is None:
            salida.append("")
        elif clave in primera:
            salida.append(primera[clave])
        else:
            primera[clave] = str(e.get("origen") or "")
            salida.append("")
    return salida


def analizar(doc, mi_nif="", forzada="", duplicada_de=""):
    """Todo lo que un conector necesita de una factura, ya calculado y con sus motivos de revisión.
    `duplicada_de`: el origen de la primera entrada del lote con la misma factura (lo da `duplicadas`)."""
    fac = doc.get("factura") or {}
    dirn = direccion(doc, mi_nif, forzada)
    emisor, receptor, contraparte = partes(doc, dirn)
    regs = registros_iva(doc)
    pct_ret, cuota_ret = retencion(doc)
    base, iva, total = decimal(fac.get("base_total")), decimal(fac.get("iva_total")), decimal(fac.get("total_factura"))
    suplidos = redondear(sum((r["base"] for r in regs if r["clase"] == "NO_SUJETO"), Decimal("0")))
    recargo_regs = redondear(sum((r["cuota_recargo"] or Decimal("0") for r in regs), Decimal("0")))
    dado = recargo_dado(doc)
    recargo = dado if dado is not None else recargo_regs        # v1.1: manda el que viene; si no, el calculado
    rect = fac.get("factura_rectificada") or {}

    motivos = []
    def motivo(codigo, texto):
        motivos.append((codigo, texto))

    codigos_aviso = {str((a or {}).get("codigo") or "") for a in doc.get("avisos") or []}
    if dirn == DESCONOCIDA:
        motivo("DIRECCION_DESCONOCIDA", "no se sabe si es recibida o emitida: tu NIF no está en un solo lado de la factura")
    elif not forzada and mi_nif and "TIPO_DISCREPANTE" not in codigos_aviso:
        # v1.3: si la API ya lo avisa (TIPO_DISCREPANTE), sale por su aviso y no se cuenta dos veces
        por_nif = direccion_por_nif(doc, mi_nif)
        if por_nif != DESCONOCIDA and por_nif != dirn:
            motivo("DIRECCION_CONTRADICE_NIF", f"la respuesta dice {dirn} y tu NIF está solo del lado de una {por_nif}")
    numero = leido(fac.get("numero"))
    if not numero:
        motivo("NUMERO_NO_LEIDO", "la factura no trae número legible")
    fecha = fecha_valida(fac.get("fecha"))
    if not fecha:
        motivo("FECHA_NO_VALIDA", "la fecha falta o no existe en el calendario")
    if dirn != DESCONOCIDA and not contraparte["nif"]:
        motivo("NIF_CONTRAPARTE_NO_LEIDO", "falta el NIF de la otra parte")
    if base is None or iva is None or total is None:
        motivo("IMPORTES_NO_LEIDOS", "falta la base, el IVA o el total de la cabecera")
    elif base == 0 and iva == 0 and total == 0 and not doc.get("lineas"):
        motivo("SIN_IMPORTES", "la factura no trae ningún importe")
    if cuota_ret is None:
        motivo("RETENCION_SIN_IMPORTE", "hay una retención sin importe")
    descuadre = None
    if None not in (base, iva, total, cuota_ret):
        descuadre = redondear(base + iva + suplidos + recargo - cuota_ret - total)
        if abs(descuadre) >= TOLERANCIA and "DESCUADRE" not in codigos_aviso:
            # si la API ya lo avisa (DESCUADRE), sale por su aviso y no se cuenta dos veces; la cifra se calcula igual
            motivo("DESCUADRE", f"base + IVA + suplidos + recargo - retención difiere del total en {descuadre}")
    if any(r["clase"] == "AJUSTE" for r in regs):
        motivo("DESGLOSE_NO_CUADRA", "las líneas no reconstruyen la base o el IVA de la cabecera")
    if any(r["clase"] == "IVA" and r["tipo_iva"] not in TIPOS_IVA_LEGALES for r in regs):
        motivo("TIPO_IVA_DESCONOCIDO", "hay un tipo de IVA que no es uno de los vigentes")
    if bool(fac.get("tiene_recargo_equivalencia")) and any(
            r["clase"] == "IVA" and r["tipo_iva"] and r["tipo_recargo"] is None for r in regs):
        motivo("RECARGO_SIN_TIPO", "hay recargo de equivalencia sobre un tipo de IVA que no lo tiene")
    if dado is not None and abs(dado - recargo_regs) >= TOLERANCIA:
        motivo("RECARGO_SIN_DESGLOSE", f"la respuesta trae un recargo de {dado} que no se puede repartir por tipo de IVA "
                                       f"(los registros suman {recargo_regs})")
    situacion = leido((doc.get("verificacion_identidad") or {}).get("situacion")).upper()
    if situacion in SITUACIONES_NIF_GRAVES:
        motivo(MOTIVO_POR_SITUACION_NIF[situacion], f"la AEAT da el NIF {situacion}: no se contabiliza sin comprobarlo")
    divisa = leido(fac.get("divisa")).upper()
    if divisa and divisa != DIVISA_DE_LOS_PROGRAMAS:
        motivo("DIVISA_NO_EUR", f"la factura viene en {divisa}: los importes no están en euros y no se convierten")
    for aviso in doc.get("avisos") or []:
        motivo(f"AVISO_{aviso.get('codigo')}", str(aviso.get("mensaje") or ""))
    for h in doc.get("dictamen_auditoria") or []:
        if h.get("gravedad") in ("rechazado", "aviso"):
            motivo(f"DICTAMEN_{h.get('codigo')}", str(h.get("mensaje") or ""))
    if duplicada_de:
        motivo("POSIBLE_DUPLICADA", f"misma dirección, NIF de la contraparte, número y fecha que {duplicada_de}: "
                                    "la misma factura subida dos veces; no se contabiliza sin mirarla")
    aj = ajuste_redondeo(doc)
    notas = []
    if aj:
        notas.append(("AJUSTE_REDONDEO", f"las líneas difieren de la cabecera en {aj['dif_base']} de base y "
                                         f"{aj['dif_cuota']} de cuota: redondeo, fundido en la línea y el registro "
                                         f"del {texto_tipo(aj['tipo_iva'])} %"))

    return {
        "direccion": dirn, "numero": numero, "fecha": fecha, "divisa": divisa,
        "emisor": emisor, "receptor": receptor, "contraparte": contraparte,
        "es_rectificativa": bool(fac.get("es_rectificativa")),
        "rectifica_numero": leido(rect.get("numero")), "rectifica_fecha": fecha_valida(rect.get("fecha")),
        "base": redondear(base) if base is not None else None, "iva": redondear(iva) if iva is not None else None,
        "suplidos": suplidos,
        "recargo": recargo if (dado is not None or bool(fac.get("tiene_recargo_equivalencia"))) else None,
        "retencion": cuota_ret, "porcentaje_retencion": pct_ret,
        "total": redondear(total) if total is not None else None,
        "descuadre": descuadre, "cuadra": descuadre is not None and abs(descuadre) < TOLERANCIA,
        "registros": regs,
        "estado_nif_aeat": leido((doc.get("verificacion_identidad") or {}).get("situacion")),
        "avisos": [str(a.get("codigo")) for a in doc.get("avisos") or []],
        "dictamen": [f"{h.get('codigo')}:{h.get('gravedad')}" for h in doc.get("dictamen_auditoria") or []],
        "primer_concepto": next((leido(l.get("descripcion_original")) or leido(l.get("short_name"))
                                 for l in doc.get("lineas") or [] if leido(l.get("descripcion_original"))
                                 or leido(l.get("short_name"))), ""),
        "ajuste_redondeo": aj, "notas": notas,
        "motivos": motivos, "revisar": bool(motivos),
    }


# ─── ayudas opcionales (v1.3): `analizar` NO las llama ────────────────────────
#
# La clave de operación de una compra a un proveedor extranjero y el maestro de terceros no vienen en `v1`. Son
# ayudas para el conector que las necesite; ninguna cambia lo que devuelve `analizar`.

BIEN, SERVICIO = "BIEN", "SERVICIO"
ADQUISICION_INTRACOMUNITARIA_BIENES = "ADQUISICION_INTRACOMUNITARIA_BIENES"
SERVICIO_INTRACOMUNITARIO_ISP = "SERVICIO_INTRACOMUNITARIO_ISP"
IMPORTACION_BIENES = "IMPORTACION_BIENES"
SERVICIO_EXTRACOMUNITARIO_ISP = "SERVICIO_EXTRACOMUNITARIO_ISP"

# Concepto -> cuenta del PGC. El ORDEN importa: gana la primera que casa, y por eso las acciones de servicio van
# antes que los objetos físicos (manda lo que se hace, no aquello sobre lo que se hace: «reparación de bomba» es
# un servicio). Portado tal cual del clasificador del portal de MuninCloud, que lo midió sobre sus bancos.
_REGLAS_CUENTA = [(re.compile(p, re.ASCII), cuenta) for p, cuenta in (
    (r"\b(materias? primas?|palets?|cajas de carton|embalajes?)\b", "601"),
    (r"\b([6-9]\d{8})\b|\b(telefonia|telefono fijo|fibra optica|tarifa plana|telefonica|vodafone|orange|movistar)\b",
     "628"),
    (r"\b(alquiler\w*|arrendamiento\w*|renting|leasing|coworking|cesion de uso|canon\w*)\b", "621"),
    (r"\b(reparacion\w*|reparar|mantenimiento|conservacion|revision\w*|calibracion|instalacion\w*|montaje|desmontaje"
     r"|puesta en marcha|limpieza|extintor\w*)\b", "622"),
    (r"\b(transporte\w*|flete\w*|mensajeria|paqueteria|logistica|aduana\w*|seur|mrw|dhl|ups|fedex|correos|mudanza)\b",
     "624"),
    (r"\b(honorarios|minuta|abogado\w*|procurador|notari\w*|registrador|gestoria|asesoria|consultoria|auditoria"
     r"|arancel\w*|escritura|traduccion|tlumaczenie|vista judicial|rozprawie|pozwu|pozew|opracowanie|sprawdzenie"
     r"|uslug\w*)\b", "623"),
    (r"\b(seguro\w*|poliza|prima de seguro|prima neta|mapfre|allianz|axa|responsabilidad civil|siniestro)\b", "625"),
    (r"\b(comision\w*|corretaje|intermediacion|tpv|stripe|paypal|mantenimiento de cuenta|intereses?|financiacion)\b",
     "626"),
    (r"\b(publicidad|propaganda|marketing|google ads|facebook ads|seo|folletos|vallas|merchandising)\b", "627"),
    (r"\b(luz|electricidad|agua|gas natural|suministro electrico|iberdrola|endesa|naturgy|redexis|cepsa)\b", "628"),
    (r"\b(dvd|cd-rom|blu-ray|soporte fisico|disco de instalacion)\b", "600"),
    (r"\b(cloud|nube|hosting|saas|servidor dedicado|vps|dominio|suscripcion|licencia\w*|software|workspace|g suite"
     r"|compute usage|soporte tecnico|backup|schulung|formacion|curso|seminario|webinar|training)\b", "629"),
    (r"\b(acido|disolvente|reactivo|pintura|barniz|combustible|gasoleo|gasolina|cafe en grano|aceite)\b", "601"),
    (r"\b(bomba|valvula|manguera|tornillo|tuerca|brida|rodamiento|correa|filtro|junta|repuesto\w*|piezas?|czesci"
     r"|dostawa|zamiennych)\b", "601"),
    (r"\b(silla\w*|mesa|estanteria\w*|armario|mueble\w*|taquilla|ordenador|portatil|monitor|teclado|raton|impresora"
     r"|toner|cartucho|disco duro|movil\w*|smartphone|tablet|cable\w*|bateria|cargador|router|switch|trituradora"
     r"|compresor|motor|generador|herramienta\w*|taladro|espatula\w*|cuchara\w*|probeta|matraz|papel|boligrafo"
     r"|carpeta|embalaje|mercaderia\w*|mercancia\w*|clip)\b", "600"),
)]
_CUENTAS_BIEN = frozenset(("600", "601", "602"))
# Conceptos que no deciden nada (abstenerse es lo correcto). Un SUPLIDO no es ni bien ni servicio.
_NO_DECIDE = [re.compile(p, re.ASCII) for p in (
    r"^$", r"^pedido\b", r"^order\b", r"^ref\.?\b", r"^factura\b", r"^varios$", r"^total", r"^concepto$",
    r"^importe\b", r"^anticipo", r"^a cuenta\b", r"^provision de fondos", r"^pago electronico", r"^promociones?$",
    r"^descuento\b", r"^suplido", r"^tasas?\b", r"^arancel(es)? (de )?(importacion|aduana)")]
_ACCESORIA = re.compile(r"^(portes?|envio|gastos de envio|shipping)\b.{0,24}$", re.ASCII)
_EXTRANJERO = re.compile(r"^[A-Z]{2}[A-Z0-9]")


def _sin_acentos(texto):
    t = unicodedata.normalize("NFD", "" if texto is None else str(texto))
    t = "".join(ch for ch in t if not "̀" <= ch <= "ͯ")
    return re.sub(r"\s+", " ", t.lower()).strip()


def clasificar_concepto(concepto):
    """{tipo: BIEN|SERVICIO|None, cuenta: '6xx'|None, motivo}. `tipo` (para el IVA) y `cuenta` (sugerencia del PGC)
    son independientes: una silla comprada en Alemania es un BIEN aunque se contabilice en una cuenta de servicios."""
    t = _sin_acentos(concepto)
    if not t:
        return {"tipo": None, "cuenta": None, "motivo": "sin concepto"}
    if any(p.search(t) for p in _NO_DECIDE):
        return {"tipo": None, "cuenta": None, "motivo": "el concepto no permite saber si es un bien o un servicio"}
    for patron, cuenta in _REGLAS_CUENTA:
        m = patron.search(t)
        if m:
            return {"tipo": BIEN if cuenta in _CUENTAS_BIEN else SERVICIO, "cuenta": cuenta,
                    "motivo": f"«{m.group(0).strip()}» → cuenta {cuenta} del PGC"}
    return {"tipo": None, "cuenta": None, "motivo": "concepto no reconocido por el léxico"}


def clave_operacion(doc, mi_nif="", forzada=""):
    """AYUDA OPCIONAL. La clave de una COMPRA a un proveedor no español sin IVA español: {clave|None, cuenta, motivo}.

    El país decide la mitad (UE o no: es un dato de tabla, no un juicio) y el concepto de la línea de MAYOR importe
    (sin contar portes) decide bien o servicio. Si no se puede saber, `clave` es None y `motivo` dice por qué: quien
    contabiliza la pone. Solo usa campos de `v1`; `analizar` no la llama.
    """
    dirn = direccion(doc, mi_nif, forzada)
    if dirn != RECIBIDA:
        return {"clave": None, "cuenta": None, "motivo": "solo para facturas recibidas (compras)"}
    nif = partes(doc, dirn)[2]["nif"]
    if not _EXTRANJERO.match(nif) or nif.startswith("ES"):
        return {"clave": None, "cuenta": None, "motivo": "proveedor español: no procede esta clave"}
    iva = decimal((doc.get("factura") or {}).get("iva_total")) or Decimal("0")
    if abs(iva) >= Decimal("0.005"):
        return {"clave": None, "cuenta": None, "motivo": "la factura repercute IVA español: es una operación interior"}
    candidatas = []
    for linea in doc.get("lineas") or []:
        linea = linea or {}
        concepto = linea.get("descripcion_original") or linea.get("short_name")
        if _ACCESORIA.match(_sin_acentos(concepto)):
            continue
        c = clasificar_concepto(concepto)
        if c["tipo"] is not None:
            candidatas.append((abs(decimal(linea.get("amount")) or Decimal("0")), c))
    if not candidatas:
        return {"clave": None, "cuenta": None, "motivo": "ninguna línea permite decidir si es un bien o un servicio"}
    candidatas.sort(key=lambda x: -x[0])          # estable: a igual importe, la primera línea
    c = candidatas[0][1]
    en_la_ue = nif[:2] in PAISES_UE
    if c["tipo"] == BIEN:
        clave = ADQUISICION_INTRACOMUNITARIA_BIENES if en_la_ue else IMPORTACION_BIENES
    else:
        clave = SERVICIO_INTRACOMUNITARIO_ISP if en_la_ue else SERVICIO_EXTRACOMUNITARIO_ISP
    return {"clave": clave, "cuenta": c["cuenta"],
            "motivo": f"proveedor de {nif[:2]} ({'UE' if en_la_ue else 'fuera de la UE'}) y "
                      f"{'un bien' if c['tipo'] == BIEN else 'un servicio'}: {c['motivo']}"}


# El grupo del PGC de la contraparte: 400 acreedores en una compra, 430 clientes en una venta.
PREFIJO_COMPRAS, PREFIJO_VENTAS = "400", "430"
_IDENTIFICADOR = re.compile(r"^[^\s;,]+$")     # cualquier id sin espacios: Odoo exporta un External ID no numérico


def prefijo_contraparte(dirn):
    """AYUDA OPCIONAL. '400' para una RECIBIDA, '430' para una EMITIDA, '' si no se sabe."""
    return PREFIJO_COMPRAS if dirn == RECIBIDA else PREFIJO_VENTAS if dirn == EMITIDA else ""


def ajustar_longitud(cuenta, longitud):
    """AYUDA OPCIONAL. Una cuenta a la longitud del programa, con ceros entre el grupo y el correlativo
    (629 -> 62900000). Solo los dígitos; '' si no hay ninguno; si ya es tan larga o más, tal cual."""
    c = re.sub(r"\D", "", "" if cuenta is None else str(cuenta))
    if not c or len(c) >= longitud:
        return c
    return c + "0" * (longitud - len(c))


def leer_maestro(ruta, formato=None, maximo=None, que="identificador", no_valido="", demasiado_largo=""):
    """AYUDA OPCIONAL. Fichero «NIF;cuenta» o «NIF;id» (también vale ',' o tabulador) -> {nif normalizado: valor}.

    `formato` es la expresión que tiene que cumplir el valor (por defecto, cualquier id sin espacios, porque un
    maestro de Odoo no es numérico); `maximo`, su longitud máxima (por defecto ninguna: un External ID de Odoo pasa
    de 20; a3 pide 20). La primera línea se toma por cabecera si su valor no cumple el formato o su NIF no tiene ni
    un dígito. Las líneas vacías y las que empiezan por '#' no cuentan.

    La primera columna se normaliza SIEMPRE como un NIF (`normalizar_nif`): mayúsculas, sin espacios, puntos, guiones
    ni nada que no sea letra o cifra, y sin el prefijo ES de un NIF español. No sirve, por tanto, para claves numéricas
    con decimales: «1.5» y «15» serían la misma clave.
    """
    formato = formato or _IDENTIFICADOR
    valores = {}
    if not ruta:
        return valores
    with open(ruta, encoding="utf-8-sig") as fh:
        for n, linea in enumerate(fh, 1):
            linea = linea.strip()
            if not linea or linea.startswith("#"):
                continue
            columnas = [p.strip().strip('"') for p in re.split(r"[;,\t]", linea)]
            if len(columnas) < 2:
                raise ValueError(f"{ruta}:{n}: se esperaban dos columnas, NIF y {que}")
            nif, valor = normalizar_nif(columnas[0]), columnas[1]
            if not formato.match(valor):
                if n == 1:
                    continue        # una cabecera
                raise ValueError(f"{ruta}:{n}: «{valor}» {no_valido or f'no es un {que} válido'}")
            if n == 1 and not re.search(r"\d", nif):
                continue            # una cabecera cuyo valor también pasaría por un id («NIF;id»)
            if maximo and len(valor) > maximo:
                raise ValueError(f"{ruta}:{n}: {demasiado_largo or f'el {que} tiene más de {maximo} caracteres'}")
            valores[nif] = valor
    return valores


# ─── salida ───────────────────────────────────────────────────────────────────

_PELIGROSA = re.compile(r"^[=+\-@\t\r]")
_NUMERO = re.compile(r"^-?\d+([.,]\d+)?$")


def celda_segura(valor):
    """Un texto que una hoja de cálculo no ejecute: apóstrofo delante si parece una fórmula.

    El texto de una factura lo escribe un tercero y el fichero lo abre otro en su hoja de cálculo. Un importe
    negativo empieza por '-' y es un número: ese no se toca.
    """
    t = "" if valor is None else str(valor)
    return "'" + t if (_PELIGROSA.match(t) and not _NUMERO.match(t)) else t


def importe(valor, coma_decimal=False):
    """Dos decimales, punto (o coma) decimal, sin separador de miles. '' si no hay valor."""
    if valor is None:
        return ""
    t = format(redondear(valor), "f")
    return t.replace(".", ",") if coma_decimal else t
