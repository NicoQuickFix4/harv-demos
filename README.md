# harv-demos

Auto-gegenereerde demo HTML bestanden voor Harv Agency outreach.

## Structuur

```
public/
  demo/
    [slug]/
      index.html   ← gegenereerde demo per lead
```

## Deployment

Verbonden met Vercel. Elke push naar `main` triggert automatisch een nieuwe deploy.

Demo URLs: `https://demos.harvagency.com/demo/[slug]/`

## Gebruik

Elke demo wordt gegenereerd door de Demo Generator en geplaatst in de juiste map. De slug is gekoppeld aan het lead-ID in het CRM.
