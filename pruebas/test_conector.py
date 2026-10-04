"""Pruebas del conector de Holded. Solo biblioteca estándar:

    python3 -m unittest discover -s pruebas -v

Plantilla del kit de conectores de MuninCloud. Copia como `pruebas/test_conector.py`, cambia `CONECTOR` y `SALIDAS`,
y añade las pruebas propias del programa al final. Las tres de arriba son las mínimas de todo conector.
"""
import contextlib
import csv
import hashlib
import io
import os
import shutil
import sys
import tempfile
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
import munincloud_v1 as v1  # noqa: E402

CONECTOR = "munincloud_holded"          # el módulo del conector, sin .py
SALIDAS = ("holded_compras.csv", "holded_compras_excel_es.csv", "holded_revisar.csv", "revisar.csv", "omitidos.csv")          # los ficheros de ejemplos/salida/ que se comparan por md5
REVISAR = ("revisar.csv", "documento", ("nivel", "REVISAR"))  # fichero de revisar, columna del documento y filtro (None, o
                                            # ("revisar", "SI") si el fichero lleva todas y marca las de revisar)
ENTRADA = os.path.join(RAIZ, "ejemplos", "entrada")
SALIDA_REF = os.path.join(RAIZ, "ejemplos", "salida")
DATOS = os.path.join(RAIZ, "pruebas", "datos")
MI_NIF = "B00000018"   # NIF reservado por la AEAT para pruebas

conector = __import__(CONECTOR)
_TEMPORALES = []


def tearDownModule():
    for d in _TEMPORALES:
        shutil.rmtree(d, ignore_errors=True)


def generar(entradas, *extra):
    salida = tempfile.mkdtemp(prefix=CONECTOR + "_")
    _TEMPORALES.append(salida)
    with contextlib.redirect_stdout(io.StringIO()):
        conector.main(["--mi-nif", MI_NIF, "--cuenta-gasto", "629", "--salida", salida, *extra, *entradas])
    return salida


def documentos(ruta, columna, solo_si=None):
    if not os.path.exists(ruta):
        return set()
    with open(ruta, encoding="utf-8-sig", newline="") as fh:
        return {f.get(columna, "") for f in csv.DictReader(fh, delimiter=";")
                if solo_si is None or f.get(solo_si[0]) == solo_si[1]}


def md5(ruta):
    with open(ruta, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


class Kit(unittest.TestCase):
    """Las mínimas del kit: la salida es la del ejemplo, lo desafinado se aparta, lo que la pieza aparta no se pierde."""

    def test_salida_igual_a_la_del_ejemplo(self):
        salida = generar([ENTRADA])
        for nombre in SALIDAS:
            self.assertEqual(md5(os.path.join(salida, nombre)), md5(os.path.join(SALIDA_REF, nombre)), nombre)

    def test_lo_desafinado_sale_a_revisar(self):
        for nombre in sorted(os.listdir(DATOS)):
            if not nombre.startswith("desafinada__"):
                continue
            salida = generar([os.path.join(DATOS, nombre)])
            self.assertIn(nombre, documentos(os.path.join(salida, REVISAR[0]), REVISAR[1], REVISAR[2]), nombre)

    def test_lo_que_la_pieza_aparta_sale_a_revisar(self):
        salida = generar([ENTRADA])
        apartados = documentos(os.path.join(salida, REVISAR[0]), REVISAR[1], REVISAR[2])
        for e in v1.leer_entradas([ENTRADA]):
            if e.get("documento") and v1.analizar(e["documento"], MI_NIF)["revisar"]:
                self.assertIn(e["origen"], apartados, e["origen"])


# ─── las propias de Holded (portadas de las pruebas del portal, tests/ocr/holded.test.ts, y las decisiones) ─────

import copy  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402

CLAVES = os.path.join(RAIZ, "ejemplos", "claves_iva_ejemplo.csv")
VIA_B = os.path.join(RAIZ, "pruebas", "via_b.py")
COL = {n: i for i, n in enumerate(conector.CABECERA)}


def filas_holded(ruta):
    if not os.path.exists(ruta):
        return []
    with open(ruta, encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh, delimiter=";"))[1:]


def de(salida, nombre="holded_compras.csv", numero=None):
    filas = filas_holded(os.path.join(salida, nombre))
    return [f for f in filas if numero is None or f[0] == numero]


def motivos(salida, documento):
    with open(os.path.join(salida, "revisar.csv"), encoding="utf-8", newline="") as fh:
        for f in csv.DictReader(fh, delimiter=";"):
            if f["documento"] == documento:
                return f["motivos"].split(" | ")
    return []


def respuesta(nombre="compras__identidad.json"):
    with open(os.path.join(ENTRADA, nombre), encoding="utf-8") as fh:
        return json.load(fh)


def lote(**cambios):
    """Una respuesta sintética (desde compras__identidad) con `factura`, `lineas`… cambiados; devuelve el directorio."""
    d = copy.deepcopy(respuesta())
    for k, v in cambios.items():
        if k == "factura":
            d["documento"]["factura"].update(v)
        else:
            d["documento"][k] = v
    dir_ = tempfile.mkdtemp(prefix="holded_lote_")
    _TEMPORALES.append(dir_)
    with open(os.path.join(dir_, "sintetica.json"), "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)
    return dir_


def linea(amount, tax, qty=1.0, rate="igual", **extra):
    return dict({"descripcion_original": "Servicio", "qty": qty, "rate": amount if rate == "igual" else rate,
                 "amount": amount, "tax_percent": tax}, **extra)


class Holded(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.sin = generar([ENTRADA])
        cls.con = generar([ENTRADA], "--claves-iva", CLAVES)

    def test_cabecera_diez_obligatorias_y_las_cuatro_opcionales(self):
        with open(os.path.join(self.sin, "holded_compras.csv"), encoding="utf-8") as fh:
            cab = fh.readline().rstrip("\n").split(";")
        self.assertEqual(cab, conector.CABECERA)
        self.assertEqual(len(conector.OBLIGATORIAS), 10)
        for c in ("Operación", "Núm. cuenta de gasto", "Retención %", "Inv. Suj. Pasivo (1/0)"):
            self.assertIn(c, cab)

    def test_ninguna_obligatoria_vacia_en_lo_importable(self):
        for salida in (self.sin, self.con):
            for f in de(salida):
                for c in conector.OBLIGATORIAS:
                    self.assertNotEqual(f[COL[c]], "", c)

    def test_operacion_dentro_de_los_seis(self):
        for salida in (self.sin, self.con):
            for f in de(salida):
                self.assertIn(f[COL["Operación"]], conector.OPERACIONES)
            for f in de(salida, "holded_revisar.csv"):
                self.assertIn(f[COL["Operación"]], conector.OPERACIONES + ("",))

    def test_nacional_general_intra_impexp_receq_y_suplido_solo_su_linea(self):
        op = lambda salida, fichero, numero: [f[COL["Operación"]] for f in de(salida, fichero, numero)]  # noqa: E731
        self.assertEqual(op(self.sin, "holded_compras.csv", "P-2026-0925"), ["general", "general"])
        self.assertEqual(op(self.con, "holded_compras.csv", "FR-2026-0412"), ["intra", "intra"])
        self.assertEqual(op(self.sin, "holded_revisar.csv", "CH-2026-77"), ["impexp"])
        self.assertEqual(op(self.sin, "holded_compras.csv", "H-2026-0102"), ["receq"])
        self.assertEqual(op(self.con, "holded_compras.csv", "P-2026-0926"), ["general", "nosujeto"])

    def test_retencion_solo_en_la_primera_y_cobrada_a_cero_en_todas(self):
        f = de(self.sin, numero="H-2026-0101")
        self.assertEqual([x[COL["Retención %"]] for x in f], ["15", ""])
        self.assertEqual([x[COL["Cantidad cobrada"]] for x in f], ["0", "0"])

    def test_isp_vacia_nunca_cero(self):
        for f in de(self.sin) + de(self.sin, "holded_revisar.csv"):
            self.assertEqual(f[COL["Inv. Suj. Pasivo (1/0)"]], "")

    def test_cuenta_de_gasto_a_ocho_y_vacia_si_no_se_da(self):
        self.assertEqual({f[COL["Núm. cuenta de gasto"]] for f in de(self.sin)}, {"62900000"})
        salida = tempfile.mkdtemp(prefix="holded_")
        _TEMPORALES.append(salida)
        with contextlib.redirect_stdout(io.StringIO()):
            conector.main(["--mi-nif", MI_NIF, "--salida", salida, ENTRADA])
        self.assertEqual({f[COL["Núm. cuenta de gasto"]] for f in de(salida)}, {""})

    def test_las_ventas_no_entran_en_ningun_fichero_de_holded(self):
        numeros = {f[0] for n in ("holded_compras.csv", "holded_revisar.csv") for f in de(self.con, n)}
        for v in ("V-2026/0925", "F7-V-0001", "F7-V-0002", "F7-V-0003"):
            self.assertNotIn(v, numeros)
        self.assertIn("HOLDED_VENTA_SIN_PLANTILLA", motivos(self.con, "ventas__mixta.json"))

    def test_sin_tabla_solo_p_iva_21_y_el_resto_a_revisar(self):
        self.assertEqual({f[COL["IVA %"]] for f in de(self.sin)}, {"p_iva_21"})
        self.assertIn("HOLDED_CLAVE_IVA_SIN_TABLA", motivos(self.sin, "compras__mixta_irpf.json"))
        self.assertEqual([f[COL["IVA %"]] for f in de(self.con, numero="F7-C-0001")], ["p_iva_21", "p_iva_10"])

    def test_tipo_no_entero_a_revisar_sin_tabla_y_con_la_suya_entra(self):
        self.assertIn("HOLDED_CLAVE_IVA_SIN_TABLA", motivos(self.con, "holded__tipo_no_entero.json"))
        tabla = os.path.join(tempfile.mkdtemp(prefix="holded_"), "claves.csv")
        _TEMPORALES.append(os.path.dirname(tabla))
        with open(tabla, "w", encoding="utf-8") as fh:
            fh.write("7.5;p_iva_7_5\n")
        salida = generar([os.path.join(ENTRADA, "holded__tipo_no_entero.json")], "--claves-iva", tabla)
        self.assertEqual([f[COL["IVA %"]] for f in de(salida)], ["p_iva_7_5"])

    def test_extranjero_sin_clasificar_operacion_vacia_nunca_general(self):
        f = de(self.con, "holded_revisar.csv", "H-2026-0104")
        self.assertEqual([x[COL["Operación"]] for x in f], [""])
        self.assertIn("HOLDED_OPERACION_SIN_CLASIFICAR", motivos(self.con, "holded__extranjero_sin_clasificar.json"))

    def test_sin_precio_va_una_unidad_por_el_importe_nunca_cero(self):
        f = de(self.sin, numero="H-2026-0103")
        self.assertEqual((f[0][COL["Precio unidad"]], f[0][COL["Unidades"]]), ("100.00", "1"))

    def test_una_fila_por_linea_y_sin_fila_de_ajuste(self):
        entradas = v1.leer_entradas([ENTRADA])
        dup = v1.duplicadas(entradas, MI_NIF, "")
        for e, duplicada_de in zip(entradas, dup):
            if not e["documento"] or duplicada_de:
                continue
            numero = e["documento"]["factura"]["numero"]
            veces = 1 + dup.count(e["origen"])      # la pareja duplicada: la 1.ª en compras, la 2.ª en revisar
            filas = [f for n in ("holded_compras.csv", "holded_revisar.csv") for f in de(self.con, n, numero)]
            if filas:
                self.assertEqual(len(filas), veces * len(e["documento"].get("lineas") or []), numero)

    def test_duplicada_y_redondeo(self):
        self.assertEqual(len(de(self.con, "holded_compras.csv", "P-2026-3001")), 2)       # la 1.ª, una vez
        self.assertTrue(all(f[5].startswith("[REV] ") for f in de(self.con, "holded_revisar.csv", "P-2026-3001")))
        self.assertIn("POSIBLE_DUPLICADA", motivos(self.con, "compras__duplicada_b.json"))
        self.assertEqual(motivos(self.con, "compras__duplicada_a.json"), [])
        # 1 céntimo de base: a la línea mayor (la primera de las tres iguales); Precio x Unidades = la base de cabecera
        self.assertEqual([f[6] for f in de(self.con, "holded_compras.csv", "P-2026-3002")], ["25.52", "25.51", "25.51"])
        self.assertEqual(motivos(self.con, "compras__redondeo_base.json"), ["AJUSTE_REDONDEO"])
        self.assertEqual(motivos(self.con, "compras__redondeo_cuota.json"), ["AJUSTE_REDONDEO"])
        self.assertIn("DESGLOSE_NO_CUADRA", motivos(self.con, "compras__descuadre_3c.json"))
        self.assertEqual(de(self.con, "holded_compras.csv", "P-2026-3004"), [])

    def test_rectificativa_exento_y_varias_retenciones_a_revisar(self):
        self.assertIn("HOLDED_RECTIFICATIVA", motivos(self.con, "compras__rectificativa.json"))
        s = generar([lote(lineas=[linea(100.0, 0.0)], factura={"base_total": 100.0, "iva_total": 0.0,
                                                                "total_factura": 100.0})], "--claves-iva", CLAVES)
        self.assertIn("HOLDED_EXENTO_O_CERO", motivos(s, "sintetica.json"))
        s = generar([lote(lineas=[linea(100.0, 21.0)], factura={"base_total": 100.0, "iva_total": 21.0, "total_factura": 104.0},
                          impuestos_y_retenciones=[{"rate": -15.0, "importe": -15.0}, {"rate": -2.0, "importe": -2.0}])])
        self.assertIn("HOLDED_VARIAS_RETENCIONES", motivos(s, "sintetica.json"))

    def test_una_formula_en_el_concepto_no_se_ejecuta(self):
        s = generar([lote(lineas=[linea(100.0, 21.0, descripcion_original="=HYPERLINK(\"x\")")],
                          factura={"base_total": 100.0, "iva_total": 21.0, "total_factura": 121.0})])
        self.assertTrue(de(s)[0][COL["Concepto"]].startswith("'="))

    def test_copia_excel_es_marca_y_coma_y_las_mismas_filas(self):
        with open(os.path.join(self.sin, "holded_compras_excel_es.csv"), "rb") as fh:
            self.assertTrue(fh.read().startswith(b"\xef\xbb\xbf"))
        with open(os.path.join(self.sin, "holded_compras.csv"), "rb") as fh:
            self.assertFalse(fh.read().startswith(b"\xef\xbb\xbf"))
        normal, excel = de(self.sin), de(self.sin, "holded_compras_excel_es.csv")
        self.assertEqual(len(normal), len(excel))
        self.assertEqual(excel[[f[0] for f in excel].index("H-2026-0103")][COL["Precio unidad"]], "100,00")

    def test_fichero_de_claves_mal_escrito_se_niega(self):
        d = tempfile.mkdtemp(prefix="holded_")
        _TEMPORALES.append(d)
        for contenido in ("9,5;p_iva_9\n", "21;p iva 21\n", "veintiuno;p_iva_21\n21;p_iva_21\nx;y\n"):
            ruta = os.path.join(d, "c.csv")
            with open(ruta, "w", encoding="utf-8") as fh:
                fh.write(contenido)
            with self.assertRaises(ValueError, msg=contenido):
                conector.leer_claves(ruta)
        with open(ruta, "w", encoding="utf-8") as fh:
            fh.write("NS;p_iva_0\n21;p_iva_21\n")
        self.assertEqual(conector.leer_claves(ruta), {"NS": "p_iva_0", "21": "p_iva_21"})

    def test_via_b_da_ok_y_se_pone_roja_si_cambia_un_importe_o_falta_una_fila(self):
        r = subprocess.run([sys.executable, "-B", VIA_B, SALIDA_REF, ENTRADA], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        for cambio in ("importe", "fila"):
            d = tempfile.mkdtemp(prefix="holded_vb_")
            _TEMPORALES.append(d)
            for n in os.listdir(SALIDA_REF):
                shutil.copy(os.path.join(SALIDA_REF, n), d)
            for n in ("holded_compras.csv", "holded_compras_excel_es.csv"):
                with open(os.path.join(d, n), encoding="utf-8", newline="") as fh:
                    lineas = fh.read().split("\n")
                if cambio == "importe":       # la primera fila de 100 x 1, esté donde esté en el lote
                    k = next(i for i, x in enumerate(lineas) if '"100";"1"' in x)
                    lineas[k] = lineas[k].replace('"100";"1"', '"101";"1"', 1)
                else:
                    del lineas[1]
                with open(os.path.join(d, n), "w", encoding="utf-8", newline="") as fh:
                    fh.write("\n".join(lineas))
            r = subprocess.run([sys.executable, "-B", VIA_B, d, ENTRADA], capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, cambio + r.stdout)


if __name__ == "__main__":
    unittest.main()
