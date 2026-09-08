# Recorded fixtures — workspaces (lium-platform DAH-2975 / DAH-2986 / DAH-3030 / DAH-3031)

Bodies as the API returns them, written from the OpenAPI of lium-platform branch `DAH-3031-workspace-invitations`
(the stack #83 → #96 → #123 → #124) and checked against its schemas when generated: every required
field present, no field the schema does not have (`WorkspaceResponse`, `WorkspaceMemberResponse`,
`WorkspaceInvitationResponse`, `PodListItemResponse`). `GET /users/me` and `POST /keys` have no
response model; their bodies follow `UserService.get_user_info` and the `ApiKey` row.

- `users_me_off.json` — a server with `WORKSPACES_ENABLED` off: no `workspace` key.
- `users_me_research.json` / `users_me_personal.json` — the key acts in the team / in the personal workspace.
- `workspaces_key.json` — `GET /workspaces` with an API key: the one workspace it acts in.
- `workspaces_session.json` — the same with a session: every workspace of the account, with the role in each.
- `members.json`, `invitation.json`, `key_created.json`, `login.json`.
- `pods_research.json` / `pods_off.json` — `GET /pods` with and without `workspace_id`.
