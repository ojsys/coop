"""
The public invoice-pay link.

An officer opens this from the invoice email with no session, so the pay token
*is* the authorisation. That shapes everything here:

* it is unauthenticated, and therefore rate limited — guessing at tokens has to
  be pointless as well as improbable;
* it discloses only what the holder already received by email (their own
  cooperative's name, the amount, the period), never a list, never another
  tenant;
* a settled or void invoice stops being payable rather than quietly taking
  money for a period already paid.
"""
from __future__ import annotations

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from platform_admin.models import Invoice


class InvoicePayView(APIView):
    """`GET/POST /public/invoice/<pay_token>/` — view and pay one invoice."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invoice_pay"

    def _invoice(self, pay_token):
        if not pay_token:
            return None
        return (Invoice.objects
                .select_related("cooperative", "subscription",
                                "subscription__plan")
                .filter(pay_token=pay_token)
                .first())

    def _summary(self, invoice):
        from django.conf import settings

        plan = invoice.subscription.plan if invoice.subscription_id else None
        return {
            "number": invoice.number,
            "cooperative": invoice.cooperative.name,
            "period_label": invoice.period_label,
            "plan": plan.name if plan else None,
            "amount": str(invoice.amount),
            "currency": invoice.currency,
            "status": invoice.status,
            "due_at": invoice.due_at.isoformat() if invoice.due_at else None,
            "payable": not invoice.is_settled,
            # For Paystack Inline, the same way the member checkout works.
            "paystack_public_key": settings.PAYSTACK_PUBLIC_KEY or None,
        }

    def get(self, request, pay_token=None):
        invoice = self._invoice(pay_token)
        if invoice is None:
            # Deliberately identical to any other bad token: distinguishing
            # "no such invoice" from "not yours" would make the endpoint a
            # membership oracle.
            return Response({"detail": "That payment link is not valid."},
                            status=404)
        return Response(self._summary(invoice))

    def post(self, request, pay_token=None):
        from payments.providers import PaymentInitError
        from payments.services import initialize_invoice_payment

        invoice = self._invoice(pay_token)
        if invoice is None:
            return Response({"detail": "That payment link is not valid."},
                            status=404)
        if invoice.is_settled:
            return Response(
                {"detail": f"Invoice {invoice.number} is already settled."},
                status=409)

        email = (invoice.cooperative.contact_email or "").strip()
        if not email:
            from communications.email import cooperative_notification_emails

            recipients = cooperative_notification_emails(invoice.cooperative)
            email = recipients[0] if recipients else ""
        if not email:
            return Response(
                {"detail": "No contact address on record for this "
                           "cooperative, so a receipt could not be issued."},
                status=400)

        payload = self._summary(invoice)
        try:
            payload["authorization_url"] = initialize_invoice_payment(
                invoice, email=email,
                callback_url=request.data.get("callback_url"),
            )
        except PaymentInitError as exc:
            # Surface the gateway's own reason; the invoice stands and they
            # can try again.
            payload["authorization_url"] = None
            payload["payment_error"] = str(exc)
        payload["payer_email"] = email
        # Paystack Inline needs the reference client-side. Read back from the
        # instance because initialize_invoice_payment mints a fresh one per
        # attempt and saves it.
        invoice.refresh_from_db(fields=["psp_reference"])
        payload["reference"] = invoice.psp_reference
        return Response(payload)


class InvoiceVerifyView(APIView):
    """`POST /public/invoice/verify/` — settle after returning from Paystack."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invoice_pay"

    def post(self, request):
        from payments.services import verify_invoice_payment

        reference = (request.data.get("reference") or "").strip()
        if not reference:
            return Response({"detail": "A payment reference is required."},
                            status=400)

        invoice = verify_invoice_payment(reference)
        if invoice is None:
            return Response({"detail": "That payment could not be matched."},
                            status=404)
        return Response({
            "number": invoice.number,
            "status": invoice.status,
            "paid": invoice.status == Invoice.Status.PAID,
        })
