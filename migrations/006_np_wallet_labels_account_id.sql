-- Mount Midas — Migration 006
-- np_wallet_labels: account_id Spalte für JOIN auf wallet_movements.to_principal (Hex Account ID)

ALTER TABLE np_wallet_labels ADD COLUMN IF NOT EXISTS account_id TEXT;
CREATE INDEX IF NOT EXISTS idx_np_wallet_labels_account_id ON np_wallet_labels (account_id);
