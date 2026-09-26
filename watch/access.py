from functools import wraps

from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.contrib.auth.views import redirect_to_login

from .models import Household, Membership


def household_view(need="view"):
    """Login + membership check. need: view | act (owner/caregiver) | manage (owner). Non-members get 404 (no leak)."""
    def deco(fn):
        @wraps(fn)
        def wrapper(request, hid, *a, **kw):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            h = get_object_or_404(Household, pk=hid)
            m = Membership.objects.filter(user=request.user, household=h).first()
            if not m:
                raise Http404()
            if (need == "manage" and not m.can_manage) or (need == "act" and not m.can_act):
                from django.http import HttpResponseForbidden
                return HttpResponseForbidden("Your role does not allow this.")
            return fn(request, h, m, *a, **kw)
        return wrapper
    return deco
