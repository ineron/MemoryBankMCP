# Task Dependencies

## Billing migration blocked by OAuth
The billing provider's webhook auth reuses the same login session, so #2
can't be verified end-to-end until #1 ships.
