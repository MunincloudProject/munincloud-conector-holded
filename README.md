# munincloud-conector-holded

**MuninCloud · conector Holded (plantilla de importación de facturas de compra).** Convierte las respuestas de la **API
de auditoría de facturas de MuninCloud** (formato `v1`) en el CSV de la plantilla de **compras** de Holded: una fila
por línea de factura, listo para *Compras › Facturas › Importar*. Solo genera el fichero: no usa la API de Holded, no
se conecta a tu cuenta y no sube nada.

Es un único fichero de Python sin dependencias (`munincloud_holded.py`, más la pieza común `munincloud_v1.py`).
Cógelo, léelo y adáptalo a tu cuenta: para eso está. Licencia MIT (`LICENSE`).

## Requisitos

Python 3.8 o posterior (solo biblioteca estándar). Probado con Python 3.12; con 3.8 se ha comprobado la sintaxis.

## En cinco minutos

```bash
python3 munincloud_holded.py --mi-nif B00000018 --cuenta-gasto 629 --salida salida ejemplos/entrada
head -3 salida/holded_compras.csv
python3 pruebas/via_b.py salida ejemplos/entrada
python3 -m unittest discover -s pruebas
```

`B00000018` es un NIF reservado por la AEAT para pruebas: pon el tuyo. La salida tiene que ser la de `ejemplos/salida/`.

Lo que escribe en `--salida`:

| fichero | qué |
|---|---|
| `holded_compras.csv` | **el que se importa**: UTF-8 sin marca, `;`, punto decimal, la cabecera sin comillas y los datos entre comillas |
| `holded_compras_excel_es.csv` | la misma información para abrirla en un Excel en español: marca UTF-8 y coma decimal |
| `holded_revisar.csv` | las compras que no conviene importar a ciegas, en el mismo formato, con `[REV]` delante del concepto |
| `revisar.csv` | qué factura se apartó y por qué (código de motivo), también las ventas |
| `omitidos.csv` | lo que no era una factura terminada |

Las columnas, en este orden: `Núm. factura`, `Fecha dd/mm/yyyy`, `Fecha de vencimiento dd/mm/yyyy`, `Nombre del
contacto`, `NIF`, `Concepto`, `Precio unidad`, `Unidades`, `IVA %`, `Cantidad cobrada` (las diez obligatorias de la
plantilla) y `Retención %`, `Inv. Suj. Pasivo (1/0)`, `Operación`, `Núm. cuenta de gasto`.

Con las claves de impuesto de tu cuenta (recomendado; mira el ejemplo `ejemplos/claves_iva_ejemplo.csv`):

```
python3 munincloud_holded.py --mi-nif TU_NIF --cuenta-gasto 629 --claves-iva mis_claves.csv --salida salida trabajos/
```

## Compra o venta: cómo lo decide

La `direccion` de la respuesta manda; si no la trae, tu NIF (`--mi-nif`): si está en el cliente, es una compra.
`--direccion RECIBIDA` la fuerza para todo el lote.

**Solo compras.** La plantilla de ventas de Holded no está verificada, así que toda venta sale a `revisar.csv` con
`HOLDED_VENTA_SIN_PLANTILLA` y no entra en ningún fichero de Holded.

## Cuándo una factura sale para revisar

Los motivos de la pieza común (número o fecha que faltan, NIF de la otra parte no leído, descuadre, desglose que no
reconstruye la cabecera, NIF de baja o no identificado en la AEAT, divisa distinta del euro, avisos de la API…) y los
propios de Holded:

| motivo | qué pasa |
|---|---|
| `HOLDED_VENTA_SIN_PLANTILLA` | es una venta: este conector solo escribe la plantilla de compras |
| `HOLDED_CLAVE_IVA_SIN_TABLA` | una línea con un tipo de IVA que no está en tu `--claves-iva`; sin ese fichero, todo tipo que no sea el 21 (la única clave documentada por Holded es `p_iva_21`), los suplidos y los tipos no enteros (7,5; 9,5) |
| `HOLDED_OPERACION_SIN_CLASIFICAR` | proveedor no español del que no se sabe si es `intra` o `impexp`: `Operación` va vacía, nunca `general` |
| `HOLDED_RECTIFICATIVA` | la plantilla no tiene columna para la factura rectificada |
| `HOLDED_EXENTO_O_CERO` | una línea nacional al 0 %: si es exenta, `Operación` es `exento`, y `v1` no lo dice |
| `HOLDED_VARIAS_RETENCIONES` | más de un tipo de retención: la columna lleva uno |
| `HOLDED_RETENCION_NO_CUADRA` | la retención no es su porcentaje ni de la base ni de la primera línea |
| `HOLDED_CONTACTO_SIN_NOMBRE` | `Nombre del contacto` es obligatoria y la respuesta no lo trae |
| `HOLDED_LINEA_SIN_IMPORTE` · `HOLDED_LINEA_A_CERO` · `HOLDED_SIN_LINEAS` | una línea sin importe, a cero, o ninguna que escribir: no se inventa un precio |

**Duplicadas y redondeo** (pieza común): si el lote trae dos veces la misma factura (dirección, NIF de la otra parte, número y fecha), la primera sigue su camino y la segunda y siguientes van a revisar con `POSIBLE_DUPLICADA` (nunca se funden); y si las líneas difieren de la cabecera en 1 o 2 céntimos (base, cuota o las dos), la diferencia se funde en la línea de mayor base, la factura entra cuadrando exacto con la cabecera y `revisar.csv` lo dice con un aviso `AJUSTE_REDONDEO`; con 3 céntimos o más, a revisar como siempre.

El conector **no arregla** nada de esto: lo enseña. Una factura que no cuadra sale con su descuadre a la vista. No hay
fila de ajuste: lo que las líneas no explican va a revisar.

Cómo rellena cada columna:

- **Una fila por línea**, y las filas de una factura seguidas (una fila en blanco entre facturas impide importar).
- `Fecha de vencimiento` = la de emisión: `v1` no trae vencimiento y la columna es obligatoria.
- `Precio unidad` y `Unidades`: los de la línea si unidades x precio da su importe; si no (o falta el precio), 1 unidad
  por el importe. Nunca un precio a 0 inventado.
- `Cantidad cobrada` = 0 en todas las filas: la factura entra sin pagar.
- `Retención %` solo en la primera fila de la factura (repetida, Holded podría multiplicarla).
- `Inv. Suj. Pasivo (1/0)` **vacía**: `v1` no trae ese dato, y un 0 afirmaría lo que no se sabe.
- `Operación` por factura: `intra` (proveedor de la UE), `impexp` (de fuera), `receq` (recargo de equivalencia, después
  de lo intracomunitario) o `general`; y `nosujeto` solo en la línea de un suplido.
- `Núm. cuenta de gasto`: la de `--cuenta-gasto`, a ocho cifras (`629` → `62900000`); sin ella, vacía.

## Lo que la respuesta `v1` no trae, y este conector no inventa

- **Las claves de impuesto de tu cuenta** (*Configuración › Facturación › Impuestos*): pásalas con `--claves-iva`, un
  fichero `tipo;clave` (`21;p_iva_21`), el tipo con **punto** decimal (`9.5`) y `NS` para suplidos y no sujeto.
- **La cuenta de gasto** (`--cuenta-gasto`).
- **La inversión del sujeto pasivo nacional**, el **vencimiento**, si una línea al 0 % es **exenta** y la factura que
  corrige una **rectificativa**.
- Holded no necesita tu maestro de proveedores: relaciona el contacto por el NIF.

## Contra qué se ha verificado

- **2026-09-14** · contra la ayuda pública de Holded (help.holded.com, artículos 7903146 «Importar tus facturas de
  compra» y 6967248) y las pruebas del portal de MuninCloud: columnas, obligatorias, valores de `Operación`, fechas
  `dd/mm/yyyy`, la clave `p_iva_21` y que una fila en blanco impide importar. **No importado en ninguna cuenta Holded.**
- **2026-10-04** · las pruebas de este repositorio y una segunda lectura del fichero que no comparte código con el conector (`pruebas/via_b.py`)
  sobre los ejemplos de `ejemplos/entrada/` (sintéticos, NIF de prueba de la AEAT).

No verificado todavía en una cuenta Holded: si el importador casa las columnas por cabecera o por posición, si quiere
punto o coma decimal (por eso van las dos copias), el resto de claves de impuesto, qué hace con el importe del
recargo, si una retención repetida se multiplica y si crea el contacto cuando el NIF no existe. Lo que no figura aquí
**no está verificado** en el programa: la salida cumple el formato documentado, nada más. El conector genera el
fichero de importación; no garantiza que Holded lo acepte.

## Pruebas

```bash
python3 -m unittest discover -s pruebas -v
python3 pruebas/via_b.py ejemplos/salida ejemplos/entrada
```

Comprueban, sobre los ejemplos y sobre las respuestas desafinadas de `pruebas/datos/`, que cada factura cuadra o sale
para revisar y que la salida es la de `ejemplos/salida/`. `pruebas/via_b.py` relee el fichero sin el código del
conector: las diez obligatorias sin vacío, `Operación` dentro de los seis valores, la retención solo en la primera
fila, ninguna venta, unidades x precio igual a la base de cada factura y ninguna clave distinta de `p_iva_21` sin tu
fichero de claves (`--claves`).

`munincloud_v1.py` es la misma pieza en todos los conectores de MuninCloud: si la cambias, cámbiala en todos.

## Marcas

Holded® es una marca de Holded. MuninCloud no está asociado a, ni certificado por, Holded.

## Licencia

MIT. Copyright (c) 2026 Bogarra Integrated SL. Ver `LICENSE`.
