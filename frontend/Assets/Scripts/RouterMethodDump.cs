using System.Linq;
using System.Reflection;
using UnityEngine;

public class RouterMethodDump : MonoBehaviour
{
    public MonoBehaviour router;

    private void Start()
    {
        if (router == null)
        {
            Debug.LogWarning("RouterMethodDump: router is null");
            return;
        }

        var t = router.GetType();
        Debug.Log("RouterMethodDump: router type = " + t.FullName);

        var names = t.GetMethods(BindingFlags.Instance | BindingFlags.Public)
            .Select(m => m.Name)
            .Distinct()
            .OrderBy(x => x)
            .ToArray();

        Debug.Log("RouterMethodDump: public methods = " + string.Join(", ", names));
    }
}
