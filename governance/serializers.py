from __future__ import annotations

from rest_framework import serializers

from governance.models import Attendance, Meeting, Resolution, Vote
from governance.services import tally


class MeetingSerializer(serializers.ModelSerializer):
    attendance_count = serializers.IntegerField(
        source="attendances.count", read_only=True)

    class Meta:
        model = Meeting
        fields = ["id", "title", "location", "agenda", "minutes",
                  "scheduled_at", "voting_opens_at", "proxy_enabled", "status",
                  "attendance_count"]


class AttendanceSerializer(serializers.ModelSerializer):
    member_no = serializers.CharField(source="membership.member_no",
                                      read_only=True)
    member_name = serializers.CharField(source="membership.user.full_name",
                                        read_only=True)

    class Meta:
        model = Attendance
        fields = ["id", "meeting", "membership", "member_no", "member_name",
                  "present", "proxy_for", "created_at"]


class ResolutionSerializer(serializers.ModelSerializer):
    results = serializers.SerializerMethodField()
    my_vote = serializers.SerializerMethodField()

    class Meta:
        model = Resolution
        fields = ["id", "meeting", "title", "description", "status",
                  "voting_mode", "outcome", "opens_at", "closes_at", "results",
                  "my_vote"]
        read_only_fields = ["status", "outcome"]

    def get_my_vote(self, obj):
        """The requesting member's choice on this resolution, or None — lets
        clients show 'you voted X' instead of active vote buttons."""
        request = self.context.get("request")
        if request is None or not request.user.is_authenticated:
            return None
        from accounts.models import Membership
        membership = Membership.all_objects.filter(
            user=request.user, cooperative=obj.cooperative).first()
        if membership is None:
            return None
        vote = Vote.all_objects.filter(resolution=obj,
                                       membership=membership).first()
        return vote.choice if vote else None

    def get_results(self, obj):
        if obj.status == Resolution.Status.DRAFT:
            return None
        t = tally(obj)
        return {
            "for": t["for"]["count"], "against": t["against"]["count"],
            "abstain": t["abstain"]["count"],
            "for_weight": str(t["for"]["weight"]),
            "against_weight": str(t["against"]["weight"]),
            "passed": t["passed"],
        }


class VoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vote
        fields = ["id", "resolution", "membership", "choice", "weight",
                  "created_at"]
        read_only_fields = ["weight"]
