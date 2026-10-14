# Integration Guide — Multi-Tenant Voice Agent

> **Who this is for:** the developer of a platform that serves **multiple restaurants**
> (or branches, outlets, or locations) from one system.
>
> **Who can skip it:** single-restaurant customers. You do not need the endpoint below.

This page is the single source of truth for the contract. The in-app Integration Guide
renders this same content.

---

## What we need from you

**One endpoint that lists your restaurants.** That is the whole requirement.

You already expose `/voice-agent/menu`, `/voice-agent/orders` and so on. We need one more
endpoint that tells us *which* restaurants exist, so our dashboard can list them and so the
voice agent knows which IDs are valid.

### Why we don't just ask you to type them in

The restaurant ID is not a label — we send it straight back to your API:

```
GET https://your-api.com/voice-agent/menu?restaurantId=spice-garden
                                                        ^^^^^^^^^^^^
                                        this must match your system exactly
```

If someone typed `spice-garden` into our dashboard but your system uses `spice_garden`, our
dashboard would look perfectly fine while every real phone call failed silently with
*"Sorry, I couldn't find the menu."* Reading the list from you removes that risk entirely.

---

## The endpoint

```http
GET /voice-agent/restaurants
Authorization: Bearer <the same token you gave us for /voice-agent/menu>
```

### Response — 200 OK

A **bare JSON array**. Each entry needs exactly two fields:

```json
[
  { "id": "spice-garden", "name": "Spice Garden" },
  { "id": "ocean-grill",  "name": "Ocean Grill"  },
  { "id": "cafe-mocha",   "name": "Cafe Mocha"   }
]
```

| Field | Type | What it is |
|---|---|---|
| `id` | string | The value we will send back as `?restaurantId=`. **Must match your system exactly.** |
| `name` | string | Human-readable name. Shown in our dashboard and spoken by the voice agent. |

Extra fields are ignored, so returning your full restaurant object is fine.

### Rules

1. **Return only the restaurants this token may access.** We call it with the token you gave us.
2. **`id` must be the same value `/voice-agent/menu?restaurantId=` accepts.** This is the
   single most important rule. If they differ, calls fail silently.
3. **Keep it fast.** We time out after 10 seconds.
4. **Deleted a restaurant?** Just stop returning it. We deactivate it on our side and keep its
   history — we never delete your data.

---

## Starter code

Each snippet is a direct translation of "select my restaurants, rename the columns to
`id` and `name`." In most systems this is a ten-line change.

### Node / Express

```js
app.get('/voice-agent/restaurants', async (req, res) => {
  // Use the same auth middleware as your other /voice-agent routes
  const rows = await db.query(
    'SELECT slug, display_name FROM restaurants WHERE active = true'
  );

  res.json(
    rows.map((r) => ({
      id: r.slug,          // must match what ?restaurantId= accepts
      name: r.display_name,
    }))
  );
});
```

### Python / FastAPI

```python
@app.get("/voice-agent/restaurants")
def list_restaurants(token: str = Depends(verify_token)):
    rows = db.query(Restaurant).filter(Restaurant.active == True).all()

    return [
        {"id": r.slug, "name": r.display_name}  # id must match ?restaurantId=
        for r in rows
    ]
```

### PHP / Laravel

```php
Route::middleware('auth:api')->get('/voice-agent/restaurants', function () {
    return Restaurant::where('active', true)
        ->get()
        ->map(fn ($r) => [
            'id'   => $r->slug,           // must match ?restaurantId=
            'name' => $r->display_name,
        ]);
});
```

---

## Common mistakes

Check these first — they cover almost every failed sync.

| Mistake | What you see | Fix |
|---|---|---|
| Wrapping the array in an object | *"We reached your API but couldn't read the list"* | Return `[...]`, not `{"restaurants": [...]}` |
| Using `restaurant_id` instead of `id` | Same as above | Rename the field to exactly `id` |
| Wrong path | *"No endpoint at ..."* | Must be `/voice-agent/restaurants` under the same base URL |
| Auth not accepted | *"Your API rejected our token"* | Use the same auth as `/voice-agent/menu` |
| `id` doesn't match your menu lookup | Sync works, but calls say *"I couldn't find the menu"* | Make `id` the exact value `?restaurantId=` expects |
| Returning numeric database IDs | Works, but brittle | Prefer stable slugs over auto-increment IDs |
| Filtering by the wrong account | *"Your API returned zero sub-tenants"* | Scope the query to the token we send |

---

## Testing it

1. Open **Integration Guide** in the dashboard (or the link your colleague sent you).
2. Click **Test my endpoint**.
3. We show exactly what we received, next to what we expected.

You can click it as many times as you like while building — it only reads, and it never
changes anything on either side.

Once it passes, click **Sync sub-tenants** and your restaurants appear in the dashboard.
