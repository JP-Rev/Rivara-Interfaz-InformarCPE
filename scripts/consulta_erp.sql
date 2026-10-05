-- CTG de ingresos de soja para informar a Visec, desde el editor SQL del ERP.
--
-- Devuelve UNA sola columna con los campos separados por ';' y una primera
-- línea de encabezado. El editor exporta en ancho fijo, y con columnas sueltas
-- los campos quedan pegados entre sí (la fecha con el peso, por ejemplo) y no
-- hay forma segura de separarlos. Así, el TXT exportado es un CSV que la app
-- "Informar CPE a Visec" lee directamente.
--
-- Cambiar SOLO las dos fechas (formato DD/MM/AAAA). La de "hasta" es
-- inclusive. Para un solo día, las dos iguales.
--
-- Consulta la sociedad con la que se entró al ERP. Si da ORA-00942, anteponer
-- el esquema a la tabla: SYSADMIN., SYSADMIN_ELN. o SYSADMIN_PRA.
SELECT LINEA FROM (
    SELECT 0 AS ORDEN, NULL AS FECHA,
           'ctg;numero_cpe;fecha_movimiento;peso_ingreso_stock;kilos_estimados;renspa;punto_ingreso;numero_ingreso;planta;cosecha;cuit_productor' AS LINEA
    FROM DUAL
    UNION ALL
    SELECT 1, I.IPL_FECHA_HORA,
           TO_CHAR(I.IPL_CTG) || ';' ||
           TO_CHAR(I.IPL_SUCURSAL_INTERNA_CPE) || LPAD(TO_CHAR(I.IPL_NUMERO_INTERNO_CPE), 8, '0') || ';' ||
           TO_CHAR(NVL(I.IPL_FECHA_HORA_CONF_ARRIBO, I.IPL_FECHA_HORA), 'YYYY-MM-DD HH24:MI:SS') || ';' ||
           TO_CHAR(ROUND(I.IPL_PESO_NETO)) || ';' ||
           TO_CHAR(ROUND(I.IPL_KILOS_ESTIMADOS)) || ';' ||
           REPLACE(TRIM(I.IPL_NUMERO_RENSPA), ';', ' ') || ';' ||
           TO_CHAR(I.IPL_PUNTO_INGRESO) || ';' ||
           TO_CHAR(I.IPL_NUMERO_INGRESO) || ';' ||
           TO_CHAR(I.IPL_PLANTA) || ';' ||
           TO_CHAR(I.IPL_COSECHA) || ';' ||
           TRIM(I.IPL_CUIT_PRODUCTOR)
    FROM INGRESOS_PLANTAS I
    WHERE I.IPL_ESPECIE = '38'
      AND I.IPL_FECHA_HORA >= TO_DATE('01/10/2026', 'DD/MM/YYYY')
      AND I.IPL_FECHA_HORA <  TO_DATE('07/10/2026', 'DD/MM/YYYY') + 1
      AND I.IPL_CTG IS NOT NULL
      AND I.IPL_PESO_NETO > 0
)
ORDER BY ORDEN, FECHA;
