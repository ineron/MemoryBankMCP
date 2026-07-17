# Developer Environment Reference

## Database Connections
`psql -U stayhug -h localhost -p 5432 -d stayhug_crm`

## Service Ports
| Service | Port |
|---------|------|
| dev server | 3000 |
| prod server | 8080 |

## Known Gotchas
Port 8080 conflicts with a local proxy — use `PORT=3001 npm run prod` instead.
