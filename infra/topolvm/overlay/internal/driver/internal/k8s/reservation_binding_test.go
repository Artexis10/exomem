package k8s

import (
	"context"
	"testing"

	topolvmv1 "github.com/topolvm/topolvm/api/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

type readyVolumeWriter struct{ client.Client }

func (w readyVolumeWriter) Create(ctx context.Context, obj client.Object, opts ...client.CreateOption) error {
	lv := obj.(*topolvmv1.LogicalVolume)
	lv.Status.VolumeID = "opaque-child-handle"
	lv.Status.PhysicalUUID = "physical-child"
	size := lv.Spec.Size.DeepCopy()
	lv.Status.CurrentSize = &size
	return w.Client.Create(ctx, obj, opts...)
}
func TestReservationSnapshotBindingSurvivesRetryAndSourceGrowth(t *testing.T) {
	ctx := context.Background()
	scheme := runtime.NewScheme()
	if err := topolvmv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kube := fake.NewClientBuilder().WithScheme(scheme).Build()
	service := &LogicalVolumeService{writer: readyVolumeWriter{kube}, getter: newRetryMissingGetter(kube, kube)}
	binding := FamilyBinding{Token: "old-request-token", ImmediateSourceUUID: "physical-source"}
	snapshot, err := service.CreateSnapshot(ctx, "node", "guarded", "source", "snapshot", "ro", resource.MustParse("20Gi"), binding)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Spec.FamilyToken != binding.Token || snapshot.Spec.ImmediateSourceUUID != binding.ImmediateSourceUUID {
		t.Fatalf("submitted binding was lost: %v", snapshot.Spec)
	}
	retry, err := service.CreateSnapshot(ctx, "node", "guarded", "source", "snapshot", "ro", resource.MustParse("30Gi"), binding)
	if err != nil {
		t.Fatal(err)
	}
	if retry.Spec.Size.Cmp(resource.MustParse("20Gi")) != 0 {
		t.Fatal("retry replaced captured size with grown source size")
	}
	if _, err := service.CreateSnapshot(ctx, "node", "guarded", "source", "snapshot", "ro", resource.MustParse("20Gi"), FamilyBinding{Token: "new-request-token", ImmediateSourceUUID: "physical-source"}); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("retry changed immutable family token: %v", err)
	}
}
func TestReservationClonePersistsSnapshotBinding(t *testing.T) {
	ctx := context.Background()
	scheme := runtime.NewScheme()
	if err := topolvmv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kube := fake.NewClientBuilder().WithScheme(scheme).Build()
	service := &LogicalVolumeService{writer: readyVolumeWriter{kube}, getter: newRetryMissingGetter(kube, kube)}
	clone, err := service.CreateVolume(ctx, "node", "guarded", "", "clone", "snapshot", 20, FamilyBinding{Token: "snapshot-bound-token", ImmediateSourceUUID: "physical-snapshot"})
	if err != nil {
		t.Fatal(err)
	}
	if clone.Spec.FamilyToken != "snapshot-bound-token" || clone.Spec.ImmediateSourceUUID != "physical-snapshot" || clone.Spec.Source != "snapshot" {
		t.Fatalf("clone did not bind its immediate snapshot: %v", clone.Spec)
	}
}
