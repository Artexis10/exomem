package driver

import (
	"context"
	"testing"

	"github.com/container-storage-interface/spec/lib/go/csi"
	topolvmv1 "github.com/topolvm/topolvm/api/v1"
	"github.com/topolvm/topolvm/internal/driver/internal/k8s"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/manager"
)

type snapshotTestManager struct {
	manager.Manager
	kube client.Client
}

func (m snapshotTestManager) GetClient() client.Client             { return m.kube }
func (m snapshotTestManager) GetAPIReader() client.Reader          { return m.kube }
func (m snapshotTestManager) GetFieldIndexer() client.FieldIndexer { return m }
func (m snapshotTestManager) IndexField(context.Context, client.Object, string, client.IndexerFunc) error {
	return nil
}

type snapshotReadyClient struct{ client.Client }

func (c snapshotReadyClient) Create(ctx context.Context, obj client.Object, opts ...client.CreateOption) error {
	volume := obj.(*topolvmv1.LogicalVolume)
	volume.Status.VolumeID = "snapshot-handle"
	size := volume.Spec.Size.DeepCopy()
	volume.Status.CurrentSize = &size
	return c.Client.Create(ctx, obj, opts...)
}

func TestSnapshotSecretCannotLoseItsFamilyBinding(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := topolvmv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	// These maps exercise the fixed CSI family fields and the legacy request without Secret data.
	for _, tc := range []struct {
		name    string
		secrets map[string]string
		code    codes.Code
	}{
		{"legacy", nil, codes.OK},
		{"family", map[string]string{"backup-family-token": "token", "immediate-source-uuid": "source"}, codes.OK},
		{"wrong key", map[string]string{"misspelled-token": "token"}, codes.InvalidArgument},
		{"missing source UUID", map[string]string{"backup-family-token": "token"}, codes.InvalidArgument},
	} {
		t.Run(tc.name, func(t *testing.T) {
			size := resource.MustParse("4Gi")
			source := &topolvmv1.LogicalVolume{ObjectMeta: metav1.ObjectMeta{Name: "source"},
				Spec:   topolvmv1.LogicalVolumeSpec{Name: "source", NodeName: "node", Size: size},
				Status: topolvmv1.LogicalVolumeStatus{VolumeID: "source-handle", CurrentSize: &size}}
			kube := fake.NewClientBuilder().WithScheme(scheme).WithObjects(source).WithIndex(
				&topolvmv1.LogicalVolume{}, "status.volumeID", func(obj client.Object) []string {
					return []string{obj.(*topolvmv1.LogicalVolume).Status.VolumeID}
				}).Build()
			service, err := k8s.NewLogicalVolumeService(snapshotTestManager{kube: snapshotReadyClient{kube}})
			if err != nil {
				t.Fatal(err)
			}
			server := controllerServerNoLocked{lvService: service}
			_, err = server.CreateSnapshot(context.Background(), &csi.CreateSnapshotRequest{
				Name: "snapshot", SourceVolumeId: "source-handle", Secrets: tc.secrets,
			})
			if status.Code(err) != tc.code {
				t.Fatalf("expected %v, got %v", tc.code, err)
			}
		})
	}
}
