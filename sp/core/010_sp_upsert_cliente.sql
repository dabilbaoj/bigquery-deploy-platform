CREATE OR REPLACE PROCEDURE `core.sp_upsert_cliente`(
  IN p_cliente_id STRING,
  IN p_email STRING,
  IN p_atributos JSON,
  IN p_limite_credito NUMERIC
)
OPTIONS (
  description = 'Crea o actualiza un cliente en la tabla maestra.'
)
BEGIN
  MERGE `core.clientes` AS destino
  USING (
    SELECT
      p_cliente_id AS cliente_id,
      p_email AS email,
      p_atributos AS atributos,
      p_limite_credito AS limite_credito
  ) AS origen
  ON destino.cliente_id = origen.cliente_id
  WHEN MATCHED THEN
    UPDATE SET
      email = origen.email,
      atributos = origen.atributos,
      limite_credito = origen.limite_credito
  WHEN NOT MATCHED THEN
    INSERT (
      cliente_id,
      email,
      atributos,
      limite_credito,
      fecha_creacion
    )
    VALUES (
      origen.cliente_id,
      origen.email,
      origen.atributos,
      origen.limite_credito,
      CURRENT_TIMESTAMP()
    );
END;

