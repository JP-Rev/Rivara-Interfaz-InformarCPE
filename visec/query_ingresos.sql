-- Ingresos a planta con CPE electrónica en un rango de fechas.
-- Parámetros: :desde y :hasta (timestamps). El script los completa.
-- Si más adelante hace falta el CUIT del corredor, RUCA, etc., agregá acá
-- los JOIN a las tablas correspondientes y devolvé la columna con un alias;
-- el script la toma si existe (ver COLUMNAS_EXTRA en generar_xls_visec.py).
SELECT *
FROM INGRESOS_PLANTA
WHERE IPL_FECHA_HORA >= :desde
  AND IPL_FECHA_HORA <  :hasta
  AND IPL_TIPO_CPE = 'CP'
ORDER BY IPL_FECHA_HORA
