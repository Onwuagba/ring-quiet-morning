def ui(request):
    """Template globals: large-type mode and the household nav."""
    u = getattr(request, "user", None)
    large = False
    if u is not None and u.is_authenticated:
        p = getattr(u, "profile", None)
        large = bool(p and p.large_type)
    return {"large_type": large}
