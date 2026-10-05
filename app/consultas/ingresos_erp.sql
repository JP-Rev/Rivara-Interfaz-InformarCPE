SELECT
    TO_CHAR(I.IPL_CTG)                                                                    AS CTG,
    TO_CHAR(I.IPL_SUCURSAL_INTERNA_CPE) || LPAD(TO_CHAR(I.IPL_NUMERO_INTERNO_CPE), 8, '0') AS NUMERO_CPE,
    TO_CHAR(NVL(I.IPL_FECHA_HORA_CONF_ARRIBO, I.IPL_FECHA_HORA), 'YYYY-MM-DD HH24:MI:SS')  AS FECHA_MOVIMIENTO,
    ROUND(I.IPL_PESO_NETO)                                                                AS PESO_INGRESO_STOCK,
    ROUND(I.IPL_KILOS_ESTIMADOS)                                                          AS KILOS_ESTIMADOS,
    I.IPL_NUMERO_RENSPA                                                                   AS RENSPA,
    I.IPL_PUNTO_INGRESO                                                                   AS PUNTO_INGRESO,
    I.IPL_NUMERO_INGRESO                                                                  AS NUMERO_INGRESO,
    I.IPL_PLANTA                                                                          AS PLANTA,
    I.IPL_COSECHA                                                                         AS COSECHA,
    I.IPL_CUIT_PRODUCTOR                                                                  AS CUIT_PRODUCTOR
FROM INGRESOS_PLANTAS I
WHERE I.IPL_ESPECIE = '38'
  AND I.IPL_FECHA_HORA >= TO_DATE('01/09/2026', 'DD/MM/YYYY')
  AND I.IPL_FECHA_HORA <  TO_DATE('07/10/2026', 'DD/MM/YYYY') + 1
  AND I.IPL_CTG IS NOT NULL
  AND I.IPL_PESO_NETO > 0
ORDER BY I.IPL_FECHA_HORA, I.IPL_PUNTO_INGRESO, I.IPL_NUMERO_INGRESO;
